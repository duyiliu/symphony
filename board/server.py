#!/usr/bin/env python3
"""Symphony issue board backed by the pi session recordings.

    /                     every issue tracked on GitHub, with run history
    /issue/<ISSUE>        the recorded runs of one issue
    /live/<ISSUE>         live view, polls for new records every second
    /api/tail/<ISSUE>     incremental JSONL -> HTML, ?offset=<bytes>
    /archive/...          exported static HTML from `session export`

The issue list comes from GitHub (the only source of truth for "every issue
Symphony manages"); the agent status comes from Symphony's own state API; the
run history comes from the pi sessions on disk.
"""
import argparse
import glob
import html
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SESS_DIR = os.environ.get("SYMPHONY_SESSIONS", "/root/symphony/sessions")
LIVE_DIR = os.environ.get("SYMPHONY_SESSIONS_LIVE", "/root/symphony/sessions-live")
HTML_DIR = os.path.join(SESS_DIR, "html")
WORKFLOW = os.environ.get("SYMPHONY_WORKFLOW", "/root/symphony/config/WORKFLOW.md")
ENV_FILE = os.environ.get("SYMPHONY_ENV", "/root/symphony/env")
STATE_URL = "http://127.0.0.1:8320/api/v1/state"
IDENTIFIER_PREFIX = "GH-"

MAX_CHUNK = 256 * 1024
HEAD_BYTES = 1024 * 1024
LIVE_WINDOW = 15.0
LIVE_RETENTION_S = 30 * 86400
TEXT_LIMIT = 4000
THINK_LIMIT = 1500
ISSUE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REPO_RE = re.compile(r'^\s*repo:\s*"([^"]+)"', re.M)
CN = timezone(timedelta(hours=8))
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

ISSUE_CACHE_TTL = 60.0
SESSION_CACHE_TTL = 20.0
_cache = {"issues": None, "issues_at": 0.0, "sessions": None, "sessions_at": 0.0}

STYLE = """
:root{--canvas:#161616;--panel:#191919;--card:#1f1f1f;--elev:#272727;--sunken:#101010;
--edge:#2a2a2a;--edge2:#3d3d3d;--fg:#fafafa;--fg2:#a1a1a1;--fg3:#6e6e6e;
--ok:#34d399;--info:#38bdf8;--warn:#fbbf24;--danger:#fb7185;--tool:#a78bfa;
--sans:"Inter Variable",Inter,system-ui,"Segoe UI","Microsoft YaHei",sans-serif;
--mono:"JetBrains Mono",ui-monospace,"Cascadia Code",Consolas,monospace}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--canvas);color:var(--fg);font:13px/1.55 var(--sans);letter-spacing:-.006em;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:inherit;text-decoration:none}
::selection{background:rgba(56,189,248,.25)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}
::-webkit-scrollbar-thumb{background:var(--edge2);background-clip:content-box;border:3px solid transparent;border-radius:999px}
::-webkit-scrollbar-thumb:hover{background:var(--fg3);background-clip:content-box}
@keyframes pulse{50%{opacity:.25}}
@keyframes rot{to{transform:rotate(360deg)}}
@keyframes drawer-in{from{transform:translateX(28px);opacity:.4}to{transform:none;opacity:1}}
@keyframes fade-in{from{opacity:0}to{opacity:1}}
.app{height:100vh;display:flex;flex-direction:column;overflow:hidden}
.topbar{flex:none;display:flex;align-items:center;gap:12px;height:44px;padding:0 16px;border-bottom:1px solid var(--edge)}
.brand{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;white-space:nowrap}
.brand svg{width:14px;height:14px;color:var(--fg2)}
.health{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--fg2);min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.health .dot{flex:none;width:6px;height:6px;border-radius:50%;background:var(--ok);animation:pulse 1.5s infinite}
.auto{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:11px;color:var(--fg3);white-space:nowrap}
.auto svg{width:12px;height:12px}
.auto a{color:var(--fg2)}
.auto a:hover{color:var(--fg);text-decoration:underline}
.head{flex:none;padding:14px 16px 10px;border-bottom:1px solid var(--edge)}
.hrow{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px}
.head h1{font-size:16px;font-weight:600;margin:0}
.head .sum{font-size:12px;color:var(--fg2)}
.head .note{margin-top:3px;font-size:10.5px;color:var(--fg3)}
.tools{flex:none;display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 16px;border-bottom:1px solid var(--edge);background:rgba(22,22,22,.96)}
.chips{display:flex;align-items:center;gap:6px;min-width:0;overflow-x:auto;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;display:inline-flex;align-items:center;gap:6px;min-height:30px;padding:5px 10px;border:1px solid var(--edge);border-radius:6px;font-size:11px;color:var(--fg2);cursor:pointer;background:transparent;white-space:nowrap;transition:color .12s,border-color .12s}
.chip:hover{border-color:var(--edge2);color:var(--fg)}
.chip.on{border-color:rgba(250,250,250,.4);background:rgba(250,250,250,.08);color:var(--fg)}
.chip b{font-weight:400;font-variant-numeric:tabular-nums;opacity:.6}
.chk{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--fg2);white-space:nowrap;cursor:pointer}
.chk input{accent-color:#fafafa;margin:0}
.search{position:relative;margin-left:auto;width:230px;max-width:44vw}
.search svg{position:absolute;left:9px;top:50%;width:12px;height:12px;transform:translateY(-50%);color:var(--fg3);pointer-events:none}
.search input{width:100%;padding:6px 9px 6px 27px;border:1px solid var(--edge);border-radius:6px;background:var(--card);color:var(--fg);font:11px var(--sans);outline:none}
.search input::placeholder{color:var(--fg3)}
.search input:focus{border-color:var(--edge2)}
[hidden]{display:none!important}
.view-tabs{display:flex;gap:6px;flex-wrap:wrap}
.completed-panel{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:16px;scrollbar-gutter:stable}
.completed-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}
.completed-note,#completed-status{color:var(--fg2);font-size:12px;overflow-wrap:anywhere}
.completed-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr));gap:12px;align-items:start}
.completed-card{min-width:0;padding:16px;overflow-wrap:anywhere}
.completed-card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.completed-badge{color:var(--ok);font-size:12px}
.completed-time{color:var(--fg2);font-size:12px;margin-top:12px}
.completed-hint{color:var(--info);font-size:12px;margin-top:8px}
.completed-description pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 var(--sans)}
.completed-description summary{cursor:pointer;margin-top:8px;min-height:32px}
.completed-card:focus-visible,.chip:focus-visible{outline:2px solid var(--info);outline-offset:2px}
.board{flex:1;min-height:0;display:flex;gap:12px;padding:12px 16px 16px;overflow-x:auto;overflow-y:hidden}
.col{display:flex;flex-direction:column;flex:1 1 0;min-width:232px;min-height:0;border:1px solid var(--edge);border-radius:8px;background:var(--panel)}
.col[hidden]{display:none}
.col.is-empty{flex:0 0 232px;align-self:flex-start;max-height:100%}
.col.is-empty .col-body{flex:none;padding-bottom:0}
.col.is-empty .empty-col{padding:14px 10px}
.col-head{flex:none;display:flex;align-items:flex-start;gap:8px;padding:10px 12px 8px}
.cdot{flex:none;width:6px;height:6px;border-radius:50%;margin-top:6px;background:var(--fg3)}
.cdot.running{background:var(--warn);animation:pulse 1.5s infinite}
.cdot.blocked{background:var(--danger)}
.cdot.retrying{background:var(--warn)}
.cdot.ready{background:var(--info)}
.cdot.awaiting{background:var(--info)}
.cdot.idle{background:var(--fg3)}
.chead{min-width:0;flex:1}
.cline{display:flex;align-items:baseline;gap:6px}
.clabel{font-size:12px;font-weight:500;color:var(--fg)}
.ccount{padding:0 5px;border:1px solid var(--edge);border-radius:4px;font-size:10.5px;color:var(--fg3);font-variant-numeric:tabular-nums}
.csub{font-size:10px;color:var(--fg3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.col-body{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:0 8px 8px;display:flex;flex-direction:column;gap:8px}
.empty-col{border:1px dashed var(--edge);border-radius:6px;padding:24px 10px;text-align:center;font-size:11px;color:var(--fg3)}
.bcard{position:relative;flex:none;border:1px solid var(--edge);border-radius:6px;background:var(--card);padding:11px 12px;cursor:pointer;overflow:hidden;transition:background .12s,border-color .12s}
.bcard:hover{border-color:var(--edge2);background:var(--elev)}
.bcard.sel{border-color:var(--edge2);background:var(--elev);box-shadow:0 0 0 1px var(--edge2)}
.bcard.live::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--ok)}
.bcard[hidden]{display:none}
.btitle{font-size:13px;font-weight:600;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.bmeta{margin-top:8px;display:flex;flex-direction:column;gap:5px;font-size:11px;color:var(--fg2)}
.brow{display:flex;align-items:center;gap:8px;min-width:0}
.bid{flex:none;font:11px var(--mono);color:rgba(56,189,248,.8)}
.bid:hover{color:var(--info);text-decoration:underline}
.bgen{flex:none;padding:0 4px;border:1px solid var(--edge);border-radius:3px;font-size:10px;color:var(--fg3)}
.bpr{flex:none;font:10.5px var(--mono);color:rgba(56,189,248,.75)}
.bpr:hover{color:var(--info);text-decoration:underline}
.ext{flex:none;display:inline-flex;width:13px;height:13px;color:var(--fg3);opacity:0;transition:opacity .1s;margin-left:auto}
.bcard:hover .ext{opacity:1}
.ext:hover{color:var(--fg)}
.ext svg{width:13px;height:13px;display:block}
.bstat{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;font-size:11px}
.berr{margin-top:5px;padding-top:5px;border-top:1px solid var(--edge);font:10.5px/1.35 var(--mono);color:rgba(251,113,133,.85);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nbe{margin-top:6px;padding:5px 7px;border-radius:4px;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.25);font:10.5px/1.4 var(--mono);color:rgba(251,113,133,.92);word-break:break-all}
.bstat svg{width:12px;height:12px;flex:none}
.bright{margin-left:auto;display:inline-flex;align-items:center;gap:4px;color:var(--fg3);font-size:10.5px;white-space:nowrap;font-variant-numeric:tabular-nums}
.spin{animation:rot 1.8s linear infinite}
.tone-running{color:var(--warn)}
.tone-blocked{color:var(--danger)}
.tone-retrying{color:var(--warn)}
.tone-ready{color:var(--info)}
.tone-awaiting{color:var(--info)}
.tone-idle{color:var(--fg2)}
#shade{position:fixed;inset:0;z-index:40;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .15s}
#shade.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;bottom:0;z-index:41;display:flex;flex-direction:column;width:620px;max-width:94vw;background:var(--panel);border-left:1px solid var(--edge);box-shadow:-24px 0 70px rgba(0,0,0,.55);opacity:0;transform:translateX(28px);pointer-events:none;transition:opacity .18s,transform .18s ease-out}
.drawer.open{opacity:1;transform:none;pointer-events:auto}
.dhead{flex:none;display:flex;align-items:flex-start;gap:10px;padding:12px 14px;border-bottom:1px solid var(--edge)}
.dclose{flex:none;display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:5px;border:none;background:transparent;color:var(--fg2);font-size:16px;line-height:1;cursor:pointer}
.dclose:hover{background:var(--elev);color:var(--fg)}
.dinfo{min-width:0;flex:1}
.dtop{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:11px}
.did{font:11px var(--mono);color:var(--info)}
.did:hover{text-decoration:underline}
.dstat{display:inline-flex;align-items:center;gap:5px;font-size:11px}
.dstat svg{width:13px;height:13px;flex:none}
.dtitle{margin-top:4px;font-size:14px;font-weight:600;line-height:1.35;word-break:break-word}
.dext{flex:none;display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:5px;color:var(--fg2)}
.dext:hover{background:var(--elev);color:var(--fg)}
.dext svg{width:14px;height:14px}
.dbody{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:12px 14px 24px}
.note-box{border:1px solid var(--edge);border-radius:6px;padding:10px 12px;font-size:12px;background:var(--card)}
.note-box .nbt{display:flex;align-items:center;gap:6px;font-weight:500}
.note-box .nbt svg{width:13px;height:13px;flex:none}
.note-box .nbb{margin-top:4px;color:var(--fg2);font-size:11px;line-height:1.55}
.note-box.running{border-color:rgba(52,211,153,.28);background:rgba(52,211,153,.06)}
.note-box.running .nbt{color:var(--ok)}
.note-box.blocked{border-color:rgba(251,113,133,.3);background:rgba(251,113,133,.06)}
.note-box.blocked .nbt{color:var(--danger)}
.note-box.retrying{border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.06)}
.note-box.retrying .nbt{color:var(--warn)}
.note-box.ready{border-color:rgba(56,189,248,.28);background:rgba(56,189,248,.06)}
.note-box.ready .nbt{color:var(--info)}
.note-box.idle{background:var(--card)}
.note-box.idle .nbt{color:var(--fg2)}
.actions{margin-top:10px;border:1px solid var(--edge);border-radius:6px;background:var(--card);padding:10px 12px}
.actions .at{margin:0;font-size:11px;font-weight:500;color:var(--fg)}
.actions .an{margin:3px 0 8px;font-size:10.5px;line-height:1.5;color:var(--fg3)}
.abtns{display:flex;flex-wrap:wrap;gap:6px}
.abtn{min-height:28px;padding:4px 10px;border-radius:5px;border:1px solid var(--edge2);background:transparent;color:var(--fg2);font:11px var(--sans);cursor:pointer;transition:background .12s,color .12s,border-color .12s}
.abtn:hover:not([disabled]){color:var(--fg);border-color:var(--fg3)}
.abtn[disabled]{opacity:.45;cursor:default}
.abtn.ok{color:var(--ok);border-color:rgba(52,211,153,.35)}
.abtn.ok:hover:not([disabled]){background:rgba(52,211,153,.1)}
.abtn.warn{color:var(--warn);border-color:rgba(251,191,36,.35)}
.abtn.warn:hover:not([disabled]){background:rgba(251,191,36,.1)}
.abtn.danger{color:var(--danger);border-color:rgba(251,113,133,.35)}
.abtn.danger:hover:not([disabled]){background:rgba(251,113,133,.1)}
.acmt{display:block;width:100%;margin:0 0 8px;padding:7px 9px;border:1px solid var(--edge2);border-radius:5px;background:var(--sunken);color:var(--fg);font:11px/1.5 var(--sans);outline:none;resize:vertical}
.acmt:focus{border-color:var(--fg3)}
.acmt::placeholder{color:var(--fg3)}
#toast{position:fixed;left:50%;bottom:24px;z-index:60;transform:translateX(-50%);padding:8px 14px;border-radius:6px;border:1px solid var(--edge2);background:var(--panel);color:var(--fg);font-size:12px;box-shadow:0 10px 34px rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .15s}
#toast.on{opacity:1}
#toast.err{color:var(--danger);border-color:rgba(251,113,133,.45)}
.mgrid{margin-top:10px;display:grid;grid-template-columns:1fr 1fr;gap:10px 20px;border:1px solid var(--edge);border-radius:6px;background:var(--card);padding:11px 12px;font-size:11px}
.mi{display:flex;flex-direction:column;gap:1px;min-width:0}
.mi span{color:var(--fg3)}
.mi b{font-family:var(--mono);font-weight:400;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpr{color:var(--info)}
.mpr:hover{text-decoration:underline}
.dnote{margin:14px 0 2px;font-size:11px;color:var(--fg3)}
.run{margin-top:8px;border:1px solid var(--edge);border-radius:6px;background:var(--card);overflow:hidden}
.run-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--edge)}
.run-idx{font:11px var(--mono);color:var(--fg)}
.run-sha{font:11px var(--mono);color:var(--fg3)}
.run-stat{display:inline-flex;align-items:center;gap:5px;font-size:11px}
.run-stat svg{width:12px;height:12px;flex:none}
.run-time{margin-left:auto;font:10.5px var(--mono);color:var(--fg2);white-space:nowrap}
.run-meta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 14px;padding:6px 12px;font:10.5px var(--mono);color:var(--fg2)}
.grow{flex:1;min-width:0}
.logbtn{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border:1px solid var(--edge2);border-radius:4px;background:transparent;color:var(--fg2);font:10px var(--sans);cursor:pointer;white-space:nowrap}
.logbtn:hover{color:var(--fg);border-color:var(--fg3)}
.logbtn svg{width:11px;height:11px}
.logbtn[aria-pressed="true"]{color:var(--info);border-color:rgba(56,189,248,.4)}
.log{border-top:1px solid var(--edge)}
.log[hidden]{display:none}
.log-tools{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:6px 12px}
.log-meta{font:10px var(--mono);color:var(--fg3);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log-meta.live{color:var(--ok)}
.log-meta.warn{color:var(--warn)}
.fbtn{display:inline-flex;align-items:center;gap:5px;padding:2px 7px;border:1px solid var(--edge2);border-radius:4px;background:transparent;color:var(--fg2);font:10px var(--sans);cursor:pointer;white-space:nowrap}
.fbtn::before{content:"●";font-size:5px;vertical-align:1px}
.fbtn[aria-pressed="true"]{color:var(--ok);border-color:rgba(52,211,153,.35)}
.log-body{margin:0 12px 10px;max-height:320px;overflow-y:auto;overscroll-behavior:contain;border:1px solid var(--edge);border-radius:4px;background:var(--sunken);padding:8px 10px;font:10.5px/1.7 var(--mono)}
.msg{margin:0}
.log-body .msg+.msg,#feed .msg+.msg{margin-top:6px}
.ln{margin:0}
.ln.say{color:var(--fg);white-space:pre-wrap;word-break:break-word}
.ln.think{margin:6px 0;padding-left:8px;border-left:2px solid var(--edge2);color:var(--fg3);font-style:italic;white-space:pre-wrap;word-break:break-word}
.ln.think .th{display:block;margin-bottom:2px;font:10px var(--mono);font-style:normal;color:var(--fg3);opacity:.8}
.ln.user{color:var(--fg2);white-space:pre-wrap;word-break:break-word}
.ln.user::before{content:"❯ ";color:var(--info)}
.ln.tool{margin:6px 0}
.ln.tool .th{color:var(--tool);white-space:pre-wrap;word-break:break-word}
.ln.tool .out{margin-top:2px;margin-left:12px;padding-left:8px;border-left:1px solid var(--edge);color:var(--fg2);white-space:pre-wrap;word-break:break-word}
.ln.tool .tm{margin-left:12px;font-size:10px;color:var(--ok)}
.ln.tool.err .tm{color:var(--danger)}
.ln.mark{margin:6px 0;font-size:10px;color:var(--fg3);opacity:.8}
.ln.mark.err{color:var(--danger);opacity:1}
.ln.tokens{margin-top:2px;font-size:10px;color:var(--fg3)}
h1{font-size:15px;font-weight:600;margin:0 0 2px}
h2{font-size:13px;font-weight:600;margin:22px 0 6px;color:var(--fg2)}
.meta{color:var(--fg3);font-size:11.5px;margin-bottom:14px}
.meta a{color:var(--info)}
.count{color:var(--fg3)}
.live{color:var(--ok)}
.idle{color:var(--fg3)}
.busy{color:var(--warn)}
.warn{color:var(--danger)}
.badge{display:inline-flex;padding:2px 7px;border-radius:99px;font-size:10px;font-weight:500;white-space:nowrap}
.badge.running{color:var(--warn);background:rgba(251,191,36,.13)}
.badge.blocked{color:#f07070;background:rgba(251,113,133,.13)}
.badge.retrying{color:var(--warn);background:rgba(251,191,36,.13)}
.badge.ready{color:var(--info);background:rgba(56,189,248,.14)}
.badge.idle{color:var(--fg3);background:rgba(255,255,255,.06)}
.tag{font-size:10px;color:var(--fg2);background:rgba(255,255,255,.05);border-radius:3px;padding:1px 5px;margin-right:4px}
.irow{display:grid;grid-template-columns:15px minmax(0,1fr) auto;gap:9px;padding:8px 14px;border-bottom:1px solid var(--edge);cursor:pointer}
.irow:hover{background:rgba(255,255,255,.03)}
.st{display:inline-flex;width:14px;height:14px;margin-top:1px;color:var(--fg3)}
.st svg{width:14px;height:14px;display:block}
.st.running{color:var(--warn)}
.st.running svg{animation:rot 1.8s linear infinite}
.st.blocked{color:var(--danger)}
.st.retrying{color:var(--warn)}
.st.ready{color:var(--info)}
.st.idle{color:var(--fg3);opacity:.7}
.ibody{min-width:0}
.ihead{display:flex;align-items:center;gap:8px;min-width:0}
.iid{flex:none;font:11px var(--mono);color:var(--fg3)}
.title{min-width:0;font-size:12.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.isub{margin-top:1px;font-size:11px;color:var(--fg3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card{display:grid;grid-template-columns:1fr auto;gap:1px 14px;align-items:center;padding:9px 14px;border-bottom:1px solid var(--edge)}
.card:hover{background:rgba(255,255,255,.03)}
.cardhead{display:flex;align-items:center;gap:8px;min-width:0}
.sub{grid-column:1;color:var(--fg3);font-size:11px;margin-top:1px;padding-left:23px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ico{display:inline-flex;width:14px;height:14px;flex:none;color:var(--fg3)}
.ico svg{width:14px;height:14px;display:block}
.right{grid-column:2;grid-row:1/3;display:flex;align-items:center;gap:12px;color:var(--fg3);font-size:11.5px}
.bar{display:flex;gap:14px;align-items:center;color:var(--fg3);font-size:11.5px;margin:8px 0 16px}
.bar b{color:var(--fg);font-weight:600}
.empty{margin:40px auto;max-width:380px;color:var(--fg3);font-size:12px;padding:20px;text-align:center;border:1px dashed var(--edge2);border-radius:8px}
pre{white-space:pre-wrap;word-break:break-word;margin:3px 0;font:11.5px/1.55 var(--mono)}
.think{color:var(--fg3)}
.tool{color:var(--tool)}
.tokens{color:var(--fg3);font-size:10.5px;margin-top:3px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:5px 10px;border-bottom:1px solid var(--edge);font-size:12px}
th{color:var(--fg3);font-weight:500}
#feed{padding:12px 16px 40px;background:var(--canvas)}
@media(max-width:640px){
 .app{height:100dvh}
 .topbar{padding:0 10px;gap:8px}.auto{gap:4px}.auto>a{max-width:100px;overflow:hidden;text-overflow:ellipsis}.auto>svg,.auto>span{display:none}
 .head{padding:10px}.head h1{font-size:16px}.head .note{font-size:11px;line-height:1.4}
 .tools{padding:8px 10px;gap:8px}.tools>.chips{display:none}
 .view-tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));width:100%;order:-1;gap:6px}
 .view-tabs .chip{justify-content:center;padding:8px 4px;font-size:13px;min-height:44px}
 .search{flex:1;min-width:0;width:auto;max-width:none}.search input{font-size:16px;min-height:44px}
 .chk{min-height:44px}.chip,.abtn,.logbtn,.fbtn{min-height:44px}
 .board{padding:10px;gap:10px;scroll-snap-type:x proximity}.col{min-width:min(84vw,340px);scroll-snap-align:start}.col.is-empty{flex-basis:232px}
 .completed-panel{padding:12px 10px;padding-bottom:max(16px,env(safe-area-inset-bottom))}
 .completed-list{grid-template-columns:minmax(0,1fr);gap:10px}.completed-card{padding:14px}.completed-card .btitle{font-size:15px}
 .drawer{width:100vw;max-width:100vw;height:100dvh;border-left:0}
 .dhead{padding:10px;padding-top:max(10px,env(safe-area-inset-top))}.dclose,.dext{min-width:44px;min-height:44px}
 .dbody{padding:12px 10px;padding-bottom:max(24px,env(safe-area-inset-bottom))}.dtitle{font-size:16px}
 .acmt{font-size:16px}.abtns .abtn{flex:1;min-width:100px;font-size:13px}.mgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .run-head{flex-wrap:wrap}.run-time{white-space:normal;overflow-wrap:anywhere}.log-body{max-height:55dvh}
}
"""

BOARD = """
<script>
const COLS = ['running', 'blocked', 'retrying', 'ready', 'awaiting', 'idle'];
const EMPTY = {
  running: '没有正在执行的 Issue',
  blocked: '没有异常状态',
  retrying: '没有等待重试的 Issue',
  ready: '没有等待派发的 Issue',
  awaiting: '没有等待合并的 PR',
  idle: '没有空闲的 Issue'
};
const MAX_MSGS = 400;

let CUR = null, CARD = null, RUNS = [], AGENT = null, TROUBLE = null, LOG = null;
let SHOW_EMPTY = localStorage.getItem('sb-show-empty') !== '0';
let PENDING_RUN = new URLSearchParams(location.search).get('run');
let BUSY = false, TOAST_TIMER = null, FORM_OPEN = false, DRAFT_COMMENT = '', WRITABLE = false;

// Legacy board credentials are no longer used. Strip old token links without
// storing their credentials; all authorization now happens through GitHub labels.
(function () {
  localStorage.removeItem('sb-token');
  const params = new URLSearchParams(location.search);
  if (!params.has('token')) { return; }
  params.delete('token');
  const query = params.toString();
  history.replaceState(null, '', location.pathname + (query ? '?' + query : ''));
})();

function toast(message, err) {
  const el = document.getElementById('toast');
  if (!el) { return; }
  el.textContent = message;
  el.className = 'on' + (err ? ' err' : '');
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(function () { el.className = ''; }, 4200);
}

const boardEl = document.getElementById('board');
const shadeEl = document.getElementById('shade');
const drawerEl = document.getElementById('drawer');
const dheadEl = document.getElementById('dhead');
const dbodyEl = document.getElementById('dbody');
WRITABLE = boardEl.getAttribute('data-writable') === '1';

const ICON = {
  running: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6' opacity='.35'/><circle class='spin' cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-dasharray='26 35.2' transform='rotate(-90 8 8)' style='transform-origin:8px 8px'/></svg>",
  blocked: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6'/><path d='M8 4.9v3.6' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'/><circle cx='8' cy='10.9' r='.95' fill='currentColor'/></svg>",
  retrying: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round' stroke-dasharray='9.5 7.5'/></svg>",
  ready: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.6'/></svg>",
  awaiting: "<svg viewBox='0 0 16 16'><circle cx='4.6' cy='4.2' r='1.6' fill='none' stroke='currentColor' stroke-width='1.6'/><circle cx='4.6' cy='11.8' r='1.6' fill='none' stroke='currentColor' stroke-width='1.6'/><circle cx='11.4' cy='8' r='1.6' fill='none' stroke='currentColor' stroke-width='1.6'/><path d='M4.6 5.8v4.4M6.2 8h3.6' fill='none' stroke='currentColor' stroke-width='1.6' stroke-linecap='round'/></svg>",
  idle: "<svg viewBox='0 0 16 16'><circle cx='8' cy='8' r='5.6' fill='none' stroke='currentColor' stroke-width='1.4' opacity='.55'/></svg>",
  ext: "<svg viewBox='0 0 16 16'><path fill='none' stroke='currentColor' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round' d='M6.5 3.5h-3v9h9v-3M9.5 3.5h3v3M12 4 8 8'/></svg>",
  file: "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2'><path d='M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'/><path d='M14 3v5h5'/></svg>"
};

function escHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
function dur(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) { return s + ' 秒'; }
  if (s < 3600) { return Math.floor(s / 60) + ' 分'; }
  return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
}
function ago(iso) {
  const t = Date.parse(iso || '');
  return isNaN(t) ? '' : dur(Date.now() - t);
}
function fmtKB(n) {
  if (!n) { return '0B'; }
  if (n < 1024) { return n + 'B'; }
  if (n < 1048576) { return (n / 1024).toFixed(1) + 'K'; }
  return (n / 1048576).toFixed(1) + 'M';
}
function tickDurations() {
  document.querySelectorAll('[data-since]').forEach(function (el) {
    const t = Date.parse(el.getAttribute('data-since') || '');
    if (!isNaN(t)) { el.textContent = '已运行 ' + dur(Date.now() - t); }
  });
}
setInterval(tickDurations, 1000);

function markSel() {
  document.querySelectorAll('.bcard[data-issue]').forEach(function (c) {
    c.classList.toggle('sel', c.getAttribute('data-issue') === CUR);
  });
}

boardEl.addEventListener('click', function (e) {
  const card = e.target.closest('.bcard[data-issue]');
  if (!card || e.target.closest('a')) { return; }
  openIssue(card.getAttribute('data-issue'));
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && CUR) { closeDrawer(); }
});

function openIssue(name) {
  CUR = name;
  RUNS = []; AGENT = null; LOG = null; TROUBLE = null;
  FORM_OPEN = false; DRAFT_COMMENT = '';
  markSel();
  syncUrl(name);
  const card = (VIEW === 'active' ? boardEl : document.getElementById('completed-list')).querySelector(".bcard[data-issue='" + name + "']");
  CARD = card ? {
    name: name,
    title: card.getAttribute('data-title') || name,
    url: card.getAttribute('data-url') || '',
    state: card.getAttribute('data-state') || 'idle',
    text: card.getAttribute('data-state-text') || '',
    agent: card.getAttribute('data-agent') === '1',
    paused: card.getAttribute('data-paused') === '1',
    pauseReason: card.getAttribute('data-pause-reason') || '',
    closed: card.getAttribute('data-closed') === '1',
    completedAt: card.getAttribute('data-completed-at') || '',
    description: card.getAttribute('data-description') || '',
    pr: card.getAttribute('data-pr') || '',
    prNum: card.getAttribute('data-pr-num') || ''
  } : { name: name, title: name, url: '', state: 'idle', text: '', agent: false, paused: false, pauseReason: '', pr: '', prNum: '' };
  dheadEl.innerHTML = "<button class='dclose' id='dclose' title='关闭'>&#8249;</button>"
    + "<div class='dinfo'><div class='dtop'>"
    + "<a class='did' href='" + escHtml(CARD.url) + "' target='_blank' rel='noopener noreferrer'>" + escHtml(CARD.name) + "</a>"
    + "<span class='dstat tone-" + escHtml(CARD.state) + "'>" + ICON[CARD.state] + escHtml(CARD.text) + "</span>"
    + "</div><div class='dtitle'>" + escHtml(CARD.title) + "</div></div>"
    + "<a class='dext' href='" + escHtml(CARD.url) + "' target='_blank' rel='noopener noreferrer' title='在 GitHub 打开'>" + ICON.ext + "</a>";
  dbodyEl.innerHTML = "<div class='dnote'>加载中…</div>";
  shadeEl.classList.add('open');
  drawerEl.classList.add('open');
  loadRuns(true);
}

function closeDrawer() {
  CUR = null; CARD = null; RUNS = []; AGENT = null; TROUBLE = null; LOG = null;
  markSel();
  syncUrl(null);
  shadeEl.classList.remove('open');
  drawerEl.classList.remove('open');
}

function syncUrl(name) {
  const url = new URL(location.href);
  if (name) { url.searchParams.set('issue', name); }
  else { url.searchParams.delete('issue'); }
  history.replaceState(null, '', url);
}

function troubleHtml() {
  if (!TROUBLE || !TROUBLE.short) { return ''; }
  const due = TROUBLE.due_at ? new Date(TROUBLE.due_at) : null;
  const dueText = due && !isNaN(due.getTime())
    ? ' · 下次 ' + due.toLocaleTimeString('zh-CN', { hour12: false })
    : '';
  return "<div class='nbe' title='" + escHtml(TROUBLE.error || TROUBLE.short) + "'>"
    + escHtml(TROUBLE.short)
    + (TROUBLE.attempt ? ' · 第 ' + TROUBLE.attempt + ' 次' : '')
    + dueText + "</div>";
}

function refreshCard() {
  if (!CUR) { return; }
  const card = (VIEW === 'active' ? boardEl : document.getElementById('completed-list')).querySelector(".bcard[data-issue='" + CUR + "']");
  if (!card) { return; }
  CARD.state = card.getAttribute('data-state') || CARD.state;
  CARD.text = card.getAttribute('data-state-text') || CARD.text;
  CARD.title = card.getAttribute('data-title') || CARD.title;
  CARD.agent = card.getAttribute('data-agent') === '1';
  CARD.paused = card.getAttribute('data-paused') === '1';
  CARD.pauseReason = card.getAttribute('data-pause-reason') || '';
  CARD.pr = card.getAttribute('data-pr') || '';
  CARD.prNum = card.getAttribute('data-pr-num') || '';
}

function actionsHtml() {
  if (!CARD) { return ''; }
  let inner;
  if (CARD.closed) {
    return "<section class='actions' id='actions'><p class='an'>Issue 已完成，可在此查看详情和本地日志。无需再次派发或批准。</p></section>";
  }
  if (!WRITABLE) {
    inner = "<p class='an'>写操作未启用：服务端未配置 GitHub 凭据（SYMPHONY_WORKFLOW / SYMPHONY_ENV）。</p>";
  } else if (CARD.agent && CARD.paused) {
    inner = "<p class='an'>agent 与 needs-human 同时存在，Symphony 已停止执行并记录异常；摘掉其中一个标签可恢复对应流程。</p>"
      + "<div class='abtns'>"
      + "<button class='abtn warn' data-act='remove' data-label='agent'" + (BUSY ? ' disabled' : '') + ">摘掉 agent</button>"
      + "<button class='abtn warn' data-act='remove' data-label='needs-human'" + (BUSY ? ' disabled' : '') + ">摘掉 needs-human</button>"
      + "</div>";
  } else if (CARD.paused) {
    inner = "<p class='an'>已暂停等待人工。" + (CARD.pauseReason ? '<br>原因：' + escHtml(CARD.pauseReason) : '<br>原因见 issue 内的暂停评论。')
      + "</p>"
      + "<div class='abtns'>"
      + "<button class='abtn ok' data-intervene='resume'" + (BUSY ? ' disabled' : '') + ">"
      + (BUSY ? '正在提交…' : '恢复执行') + "</button></div>";
  } else if (CARD.agent) {
    const running = CARD.state === 'running';
    const label = running ? '停止' : '暂停派发';
    const tone = running ? 'danger' : 'warn';
    inner = "<p class='an'>操作直接写 GitHub 标签；摘标签会在下个轮询周期终止正在运行的任务。</p>"
      + "<div class='abtns'>"
      + "<button class='abtn warn' data-intervene='pause'" + (BUSY ? ' disabled' : '') + ">转人工处理</button>"
      + "<button class='abtn " + tone + "' data-act='remove' data-label='agent'"
      + (BUSY ? ' disabled' : '') + ">" + (BUSY ? '正在提交…' : label) + "</button></div>";
  } else if (FORM_OPEN) {
    inner = "<p class='an'>重新执行前建议留一条说明：agent 启动后会读取 issue 评论，知道这次要做什么、上次为何失败。</p>"
      + "<textarea id='acomment' class='acmt' rows='3' maxlength='4000' "
      + "placeholder='可选：这次要做什么 / 上次的问题是什么'>" + escHtml(DRAFT_COMMENT) + "</textarea>"
      + "<div class='abtns'>"
      + "<button class='abtn ok' data-act='add' data-label='agent'" + (BUSY ? ' disabled' : '') + ">"
      + (BUSY ? '正在提交…' : '发布并执行') + "</button>"
      + "<button class='abtn' data-cancel='1'" + (BUSY ? ' disabled' : '') + ">取消</button>"
      + "</div>";
  } else {
    const openLabel = CARD.state === 'awaiting' ? '重新执行…' : '开始执行…';
    inner = "<p class='an'>加 agent 标签后，agent 会开发、交付，宿主自动校验 CI/风险规则并完成合并；"
      + "高风险或收敛不了时会暂停并标记 needs-human，到时在这里恢复。</p>"
      + "<div class='abtns'>"
      + "<button class='abtn ok' data-open='1'" + (BUSY ? ' disabled' : '') + ">" + openLabel + "</button></div>";
  }
  return "<section class='actions' id='actions'><p class='at'>在看板操作 GitHub 标签</p>" + inner + "</section>";
}

function renderActions() {
  const el = document.getElementById('actions');
  if (!el) { return; }
  const active = document.activeElement;
  if (active && active.id === 'acomment') { return; }
  el.outerHTML = actionsHtml();
}

async function interveneAction(op) {
  if (!CUR || BUSY) { return; }
  let comment = '';
  if (op === 'pause') {
    comment = (prompt('转人工处理 ' + CUR + '？可填写原因（可选，会作为 issue 评论发布）：') || '').trim();
    if (comment === null) { return; }
  } else if (!confirm('恢复执行 ' + CUR + '？会移除 needs-human 并重新加 agent；'
    + '合并前宿主会重新核验当前 SHA 的检查与有效审批，重贴 agent 本身不是合并授权。')) { return; }
  BUSY = true;
  renderActions();
  try {
    const res = await fetch('/api/intervene', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue: CUR, op: op, comment: comment })
    });
    let data = {};
    try { data = await res.json(); } catch (err) { }
    if (!res.ok) { throw new Error(data.error || ('HTTP ' + res.status)); }
    toast(data.message || '已提交');
    await refreshIssues();
    refreshCard();
    await loadRuns(true);
  } catch (err) {
    toast(err.message || '操作失败', true);
  } finally {
    BUSY = false;
    renderActions();
  }
}

async function labelAction(action, label) {
  if (!CUR || BUSY) { return; }
  label = label || 'agent';
  const input = document.getElementById('acomment');
  const comment = action === 'add' && input ? input.value.trim() : '';
  const running = CARD.state === 'running';
  let verb;
  if (action === 'add' && label === 'agent') {
    verb = comment
      ? '发布说明并给 ' + CUR + ' 加上 agent 标签？调度器会在下个轮询周期开始执行（改代码，可能推 PR）。'
      : '没有填写说明，直接给 ' + CUR + ' 加 agent 标签？agent 可能重复上次的做法。';
  } else if (action === 'add') {
    verb = '给 ' + CUR + ' 加 needs-human 并摘掉 agent 标签？调度器不会再派发，直到人工恢复。';
  } else if (label === 'agent') {
    verb = running
      ? '摘掉 ' + CUR + ' 的 agent 标签？调度器会在下个轮询周期终止正在运行的任务。'
      : '摘掉 ' + CUR + ' 的 agent 标签？调度器不会再派发它。';
  } else {
    verb = '摘掉 ' + CUR + ' 的 needs-human 标签？如需继续执行，请再重新加 agent。';
  }
  if (!confirm(verb)) { return; }
  if (action === 'add') { DRAFT_COMMENT = comment; }
  BUSY = true;
  renderActions();
  try {
    const res = await fetch('/api/label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ issue: CUR, action: action, label: label, comment: comment })
    });
    let data = {};
    try { data = await res.json(); } catch (err) { }
    if (!res.ok) { throw new Error(data.error || ('HTTP ' + res.status)); }
    toast(data.message || '已提交');
    FORM_OPEN = false;
    DRAFT_COMMENT = '';
    await refreshIssues();
    refreshCard();
    await loadRuns(true);
  } catch (err) {
    toast(err.message || '操作失败', true);
  } finally {
    BUSY = false;
    renderActions();
  }
}

function noteBoxHtml() {
  const st = CARD.state;
  let title = CARD.text, body = '';
  if (CARD.closed) {
    title = '已完成';
    body = '完成于 ' + escHtml(CARD.completedAt) + '（北京时间）。GitHub Issue 已关闭，不代表已独立核实 PR 合并。'
      + (CARD.description ? "<details class='completed-description'><summary>Issue 内容</summary><pre>" + escHtml(CARD.description) + '</pre></details>' : '');
  } else if (st === 'running' && AGENT) {
    title = '正在执行';
    body = '▸ ' + escHtml(AGENT.message || AGENT.event || 'agent 运行中')
      + (AGENT.event_at ? ' · ' + ago(AGENT.event_at) + '前' : '')
      + (AGENT.started_at ? " · <span data-since='" + escHtml(AGENT.started_at) + "'>已运行</span>" : '')
      + (AGENT.tokens ? ' · tokens ' + AGENT.tokens : '')
      + (RUNS.length ? '' : '<br>日志尚未生成，pi 会话开始后自动出现。');
  } else if (st === 'running') {
    title = '正在执行';
    body = 'agent 运行中，等待最新事件。';
  } else if (st === 'blocked') {
    title = '已阻塞';
    if (CARD.paused) {
      body = '已暂停等待人工处理（needs-human）。'
        + (CARD.pauseReason ? '<br>原因：' + escHtml(CARD.pauseReason) : '')
        + '<br>在 GitHub 或本页处理完后点“恢复执行”；重贴 agent 不是合并授权，合并前会重新核验当前 SHA 与审批。';
    } else {
      body = '运行失败或需要人工处理；修复后可重新加上 agent 标签让调度器派发。' + troubleHtml();
    }
  } else if (st === 'retrying') {
    title = '等待重试';
    body = '调度器会在退避窗口结束后重新尝试。' + troubleHtml();
  } else if (st === 'awaiting') {
    title = '等待合并';
    body = 'PR 已开；agent 交付后宿主会自动校验检查与风险规则并完成合并，高风险或需人工审批时会等待 Review / 暂停转人工。';
  } else if (st === 'ready') {
    title = '等待派发';
    body = 'Issue 带有 agent 标签，调度器会在下个轮询周期领取。';
  } else {
    title = '空闲';
    body = RUNS.length
      ? '最近一次运行 ' + escHtml(RUNS[0].started || '') + '。'
      : '还没有运行记录；加上 agent 标签后调度器会开始执行。';
  }
  return "<section class='note-box " + escHtml(st) + "' id='nbox'><div class='nbt'>"
    + ICON[st] + escHtml(title) + "</div><div class='nbb'>" + body + "</div></section>";
}

function metaGridHtml() {
  const latest = RUNS[0] || null;
  const items = [
    ['Issue', escHtml(CARD.name)],
    ['状态', escHtml(CARD.text)],
    ['运行次数', String(RUNS.length)],
    ['最近运行', escHtml(latest ? latest.started : '—')],
    ['最近结束', escHtml(latest ? (latest.active ? '运行中' : (latest.ended || '—')) : '—')],
    ['日志大小', escHtml(latest ? latest.size : '—')]
  ];
  if (AGENT && AGENT.tokens) { items.push(['tokens', escHtml(String(AGENT.tokens))]); }
  if (CARD.pr) {
    items.push(['PR', "<a class='mpr' href='" + escHtml(CARD.pr) + "' target='_blank' rel='noopener noreferrer'>#"
      + escHtml(CARD.prNum) + "</a>"]);
  }
  return "<section class='mgrid' id='mgrid'>" + items.map(function (it) {
    return "<div class='mi'><span>" + it[0] + "</span><b>" + it[1] + "</b></div>";
  }).join('') + "</section>";
}

function updateNoteBox() {
  const nb = document.getElementById('nbox');
  if (nb) { nb.outerHTML = noteBoxHtml(); }
  const mg = document.getElementById('mgrid');
  if (mg) { mg.outerHTML = metaGridHtml(); }
  renderActions();
  tickDurations();
}

function runHtml(r, n) {
  const state = r.active ? 'running' : 'idle';
  const text = r.active ? '运行中' : '已结束';
  const sh = escHtml(r.short);
  return "<section class='run' data-short='" + sh + "'>"
    + "<div class='run-head'><span class='run-idx'>第 " + n + " 次</span>"
    + "<span class='run-sha'>" + sh + "</span>"
    + "<span class='run-stat tone-" + state + "'>" + ICON[state] + text + "</span>"
    + "<span class='run-time'>" + escHtml(r.started) + " → " + escHtml(r.active ? '运行中' : (r.ended || '—')) + "</span></div>"
    + "<div class='run-meta'><span>" + escHtml(r.size) + "</span><span>" + r.messages + " 条消息</span>"
    + (r.live ? "<span>live</span>" : "")
    + "<span class='grow'></span>"
    + "<button class='logbtn' data-log='" + sh + "' aria-pressed='false'>" + ICON.file + "运行日志</button></div>"
    + "<div class='log' id='log-" + sh + "' hidden>"
    + "<div class='log-tools'><span class='log-meta' id='lm-" + sh + "'></span><span class='grow'></span>"
    + "<button class='fbtn' id='fb-" + sh + "' data-follow='" + sh + "' aria-pressed='true'>跟随中</button></div>"
    + "<div class='log-body' id='lb-" + sh + "'></div></div></section>";
}

function renderRuns() {
  dbodyEl.innerHTML = noteBoxHtml() + actionsHtml() + metaGridHtml()
    + "<p class='dnote'>以下为本地执行记录（运行、日志），不构成 Issue 业务状态。</p>"
    + "<div id='runs'>" + (RUNS.length
      ? RUNS.map(function (r, i) { return runHtml(r, RUNS.length - i); }).join('')
      : "<div class='dnote'>该 issue 还没有运行记录。</div>") + "</div>";
  tickDurations();
  if (LOG) { restoreLog(); }
}

function restoreLog() {
  const sec = document.getElementById('log-' + LOG.short);
  if (!sec) { LOG = null; return; }
  sec.hidden = false;
  const btn = document.querySelector(".logbtn[data-log='" + LOG.short + "']");
  if (btn) { btn.setAttribute('aria-pressed', 'true'); }
  LOG.offset = -1; LOG.busy = false; LOG.dead = false; LOG.live = true;
  const lb = document.getElementById('lb-' + LOG.short);
  if (lb) { lb.innerHTML = ''; }
  updateFollowBtn();
  pollLog();
}

function updateRunStates() {
  RUNS.forEach(function (r) {
    const sec = document.querySelector(".run[data-short='" + r.short + "']");
    if (!sec) { return; }
    const stat = sec.querySelector('.run-stat');
    const state = r.active ? 'running' : 'idle';
    if (stat) {
      stat.className = 'run-stat tone-' + state;
      stat.innerHTML = ICON[state] + (r.active ? '运行中' : '已结束');
    }
    const t = sec.querySelector('.run-time');
    if (t) { t.textContent = r.started + ' → ' + (r.active ? '运行中' : (r.ended || '—')); }
  });
}

async function loadRuns(rebuild) {
  if (!CUR) { return; }
  const issue = CUR;
  try {
    const res = await fetch('/api/runs/' + encodeURIComponent(issue));
    if (!res.ok) {
      if (rebuild && CUR === issue) { dbodyEl.innerHTML = "<div class='dnote'>无法读取运行记录</div>"; }
      return;
    }
    const data = await res.json();
    if (CUR !== issue) { return; }
    const next = data.runs || [];
    const changed = next.map(function (r) { return r.short; }).join(',') !== RUNS.map(function (r) { return r.short; }).join(',');
    RUNS = next;
    AGENT = data.agent || null;
    TROUBLE = data.trouble || null;
    refreshCard();
    if (rebuild || changed) {
      renderRuns();
      if (!LOG && PENDING_RUN && RUNS.some(function (r) { return r.short === PENDING_RUN; })) {
        openLog(PENDING_RUN);
        PENDING_RUN = null;
      } else if (!LOG && RUNS.length && RUNS[0].active) {
        openLog(RUNS[0].short);
      }
    } else {
      updateRunStates();
      updateNoteBox();
    }
  } catch (err) { }
}
setInterval(function () { if (CUR) { loadRuns(false); } }, 5000);

function openLog(short) {
  if (LOG && LOG.short === short) { return; }
  if (LOG) { closeLog(LOG.short); }
  LOG = {
    short: short, offset: -1, live: true, following: true, busy: false,
    dead: false, skipped: 0, src: Math.random().toString(36).slice(2, 10)
  };
  const sec = document.getElementById('log-' + short);
  if (sec) { sec.hidden = false; }
  const lb = document.getElementById('lb-' + short);
  if (lb) { lb.innerHTML = ''; }
  const lm = document.getElementById('lm-' + short);
  if (lm) { lm.textContent = ''; lm.className = 'log-meta'; }
  const btn = document.querySelector(".logbtn[data-log='" + short + "']");
  if (btn) { btn.setAttribute('aria-pressed', 'true'); }
  updateFollowBtn();
  pollLog();
}

function closeLog(short) {
  const sec = document.getElementById('log-' + short);
  if (sec) { sec.hidden = true; }
  const btn = document.querySelector(".logbtn[data-log='" + short + "']");
  if (btn) { btn.setAttribute('aria-pressed', 'false'); }
}

function updateFollowBtn() {
  if (!LOG) { return; }
  const b = document.getElementById('fb-' + LOG.short);
  if (!b) { return; }
  b.textContent = LOG.following ? '跟随中' : '恢复跟随';
  b.setAttribute('aria-pressed', String(!!LOG.following));
}

function trimLog(box) {
  if (!box) { return; }
  const msgs = box.querySelectorAll('.msg');
  const excess = msgs.length - MAX_MSGS;
  for (let i = 0; i < excess; i++) { msgs[i].remove(); }
  if (excess > 0 && LOG) { LOG.skipped += excess; }
}

async function pollLog() {
  if (!LOG || LOG.busy || LOG.dead) { return; }
  const short = LOG.short;
  const issue = CUR;
  LOG.busy = true;
  try {
    const sent = LOG.offset;
    const url = '/api/run/' + encodeURIComponent(issue) + '/' + short
      + '?src=' + LOG.src + (sent >= 0 ? '&offset=' + sent : '');
    const res = await fetch(url);
    if (!LOG || LOG.short !== short) { return; }
    const lb = document.getElementById('lb-' + short);
    const lm = document.getElementById('lm-' + short);
    if (res.status === 404) {
      LOG.dead = true; LOG.live = false;
      if (lm) { lm.textContent = '记录已被清理'; lm.className = 'log-meta warn'; }
      return;
    }
    if (!res.ok) { return; }
    const data = await res.json();
    if (!LOG || LOG.short !== short) { return; }
    if (data.reset) {
      if (lb) { lb.innerHTML = ''; }
      LOG.offset = -1;
      return;
    }
    const first = sent === -1;
    if (!first && data.start !== sent && lb) {
      lb.insertAdjacentHTML('beforeend', "<div class='ln mark err'>… 记录已截断，从当前位置继续 …</div>");
      LOG.skipped = 0;
    }
    LOG.offset = data.offset;
    LOG.live = data.active || !data.eof;
    if (first && data.truncated && lb) {
      lb.insertAdjacentHTML('beforeend', "<div class='ln mark'>… 更早的记录已省略，仅显示最近部分 …</div>");
    }
    if (data.ops && data.ops.length && lb) {
      for (const op of data.ops) {
        const el = lb.querySelector("[data-bid='" + op.id + "']");
        if (op.op === 'del') { if (el) { el.remove(); } }
        else if (el) { el.outerHTML = op.html; }
        else { lb.insertAdjacentHTML('beforeend', op.html); }
      }
      trimLog(lb);
    } else if (data.html && lb) {
      lb.insertAdjacentHTML('beforeend', data.html);
      trimLog(lb);
    }
    if (LOG.following && lb) { lb.scrollTop = lb.scrollHeight; }
    if (lm) {
      lm.textContent = (data.active ? '实时更新中' : '已结束')
        + ' · ' + fmtKB(data.size)
        + (LOG.skipped ? ' · 顶部已省略 ' + LOG.skipped + ' 条' : '');
      lm.className = 'log-meta' + (data.active ? ' live' : '');
    }
  } catch (err) { }
  finally { if (LOG && LOG.short === short) { LOG.busy = false; } }
}
setInterval(function () { if (LOG && LOG.live) { pollLog(); } }, 1000);

dheadEl.addEventListener('click', function (e) {
  if (e.target.closest('#dclose')) { closeDrawer(); }
});

dbodyEl.addEventListener('click', function (e) {
  if (e.target.closest('#dclose')) { closeDrawer(); return; }
  const iv = e.target.closest('[data-intervene]');
  if (iv) { interveneAction(iv.getAttribute('data-intervene')); return; }
  const act = e.target.closest('[data-act]');
  if (act) { labelAction(act.getAttribute('data-act'), act.getAttribute('data-label')); return; }
  if (e.target.closest('[data-open]')) {
    FORM_OPEN = true;
    renderActions();
    const input = document.getElementById('acomment');
    if (input) { input.focus(); }
    return;
  }
  if (e.target.closest('[data-cancel]')) {
    FORM_OPEN = false;
    DRAFT_COMMENT = '';
    renderActions();
    return;
  }
  const fb = e.target.closest('[data-follow]');
  if (fb) {
    if (LOG) {
      LOG.following = !LOG.following;
      updateFollowBtn();
      if (LOG.following) {
        const lb = document.getElementById('lb-' + LOG.short);
        if (lb) { lb.scrollTop = lb.scrollHeight; }
      }
    }
    return;
  }
  const btn = e.target.closest('[data-log]');
  if (btn) {
    const short = btn.getAttribute('data-log');
    if (LOG && LOG.short === short) { closeLog(short); LOG = null; }
    else { openLog(short); }
  }
});

dbodyEl.addEventListener('input', function (e) {
  if (e.target && e.target.id === 'acomment') { DRAFT_COMMENT = e.target.value; }
});

dbodyEl.addEventListener('wheel', function (e) {
  const lb = e.target.closest('.log-body');
  if (lb && LOG && LOG.following && e.deltaY < 0) {
    LOG.following = false;
    updateFollowBtn();
  }
}, { passive: true });

document.addEventListener('scroll', function (e) {
  const lb = e.target;
  if (!lb || !lb.classList || !lb.classList.contains('log-body')) { return; }
  if (LOG && LOG.following && lb.scrollTop + lb.clientHeight < lb.scrollHeight - 40) {
    LOG.following = false;
    updateFollowBtn();
  }
}, true);

let VIEW = 'active', COMPLETED = [], COMPLETED_DAY = '', COMPLETED_WARNING = '';
let COMPLETED_LOADING = false, COMPLETED_LOADED_AT = 0;

function renderCompleted() {
  const list = document.getElementById('completed-list');
  if (!list) { return; }
  const q = (document.getElementById('q').value || '').trim().toLowerCase().replace(/^#/, '');
  const visible = COMPLETED.filter(function (issue) {
    return (VIEW !== 'today' || issue.day === COMPLETED_DAY)
      && (!q || ('gh-' + issue.number + ' ' + issue.title).toLowerCase().includes(q));
  });
  document.getElementById('completed-status').textContent = COMPLETED_LOADING ? '正在读取已完成 Issue…' : COMPLETED_WARNING;
  document.getElementById('completed-count').textContent = (VIEW === 'today' ? COMPLETED_DAY + ' · 今日' : '历史')
    + '已完成 ' + visible.length + ' 个 Issue（北京时间）';
  list.innerHTML = visible.map(function (issue) {
    const name = 'GH-' + issue.number;
    return "<article class='bcard idle completed-card' tabindex='0' role='button' data-issue='" + name
      + "' data-title='" + escHtml(issue.title) + "' data-url='" + escHtml(issue.url)
      + "' data-state='idle' data-state-text='已完成' data-closed='1' data-completed-at='"
      + escHtml(issue.completed_at) + "' data-description='" + escHtml(issue.body || '') + "'>"
      + "<div class='completed-card-head'><span class='bid'>" + name + "</span><span class='completed-badge'>已完成</span></div>"
      + "<div class='btitle'>" + escHtml(issue.title) + "</div><div class='completed-time'>完成于 "
      + escHtml(issue.completed_at) + "</div><div class='completed-hint'>查看详情与运行日志 ›</div></article>";
  }).join('') || (COMPLETED_LOADING ? '' : "<div class='empty-col'>" + (COMPLETED_WARNING ? '暂无可显示数据，请重试。' : '当前筛选没有已完成的 Issue。') + "</div>");
  markSel();
}

async function loadCompleted(force) {
  if (COMPLETED_LOADING) { return; }
  if (!force && Date.now() - COMPLETED_LOADED_AT < 60000) { renderCompleted(); return; }
  COMPLETED_LOADING = true;
  renderCompleted();
  try {
    const res = await fetch('/api/completed');
    if (!res.ok) { throw new Error('HTTP ' + res.status); }
    const data = await res.json();
    COMPLETED = data.issues || [];
    COMPLETED_DAY = data.today || '';
    COMPLETED_WARNING = data.warning || '';
    COMPLETED_LOADED_AT = Date.now();
  } catch (err) {
    COMPLETED_WARNING = '已完成 Issue 读取失败，请点击刷新重试。' + (COMPLETED.length ? ' 当前展示上次数据。' : '');
  } finally {
    COMPLETED_LOADING = false;
    renderCompleted();
  }
}

function switchView(view) {
  if (!['active', 'today', 'history'].includes(view)) { view = 'active'; }
  VIEW = view;
  closeDrawer();
  document.getElementById('board').hidden = view !== 'active';
  document.getElementById('completed-panel').hidden = view === 'active';
  document.getElementById('showEmpty').closest('label').hidden = view !== 'active';
  document.querySelectorAll('[data-view]').forEach(function (button) {
    button.classList.toggle('on', button.dataset.view === view);
    button.setAttribute('aria-pressed', String(button.dataset.view === view));
  });
  document.querySelector('.head h1').textContent = view === 'active' ? 'Issue 工作台' : (view === 'today' ? '今日已完成 Issue' : '历史已完成 Issue');
  const note = document.querySelector('.head .note');
  if (!note.dataset.activeText) { note.dataset.activeText = note.textContent; }
  note.textContent = view === 'active' ? note.dataset.activeText : '按 Issue 完成时间展示，点击卡片在当前页查看详情和运行日志。';
  document.getElementById('q').placeholder = view === 'active' ? '搜索标题、仓库或 #编号' : '搜索已完成 Issue 标题或编号';
  document.getElementById('sum').hidden = view !== 'active';
  const url = new URL(location.href);
  url.searchParams.delete('scope');
  if (view === 'active') { url.searchParams.delete('view'); }
  else { url.searchParams.set('view', view); }
  history.replaceState(null, '', url);
  if (view !== 'active') { return loadCompleted(false); }
}

document.querySelectorAll('[data-view]').forEach(function (button) {
  button.addEventListener('click', function () { switchView(button.dataset.view); });
});
document.getElementById('completed-refresh').addEventListener('click', function () { loadCompleted(true); });
document.getElementById('completed-list').addEventListener('click', function (e) {
  const card = e.target.closest('[data-issue]');
  if (card) { openIssue(card.dataset.issue); }
});
document.getElementById('completed-list').addEventListener('keydown', function (e) {
  const card = e.target.closest('[data-issue]');
  if (card && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openIssue(card.dataset.issue); }
});
setInterval(function () { if (VIEW !== 'active') { loadCompleted(false); } }, 60000);

function applyFilter() {
  if (VIEW !== 'active') { renderCompleted(); }
  const q = (document.getElementById('q').value || '').trim().toLowerCase().replace(/^#/, '');
  COLS.forEach(function (k) {
    const body = document.getElementById('col-' + k);
    if (!body) { return; }
    let visible = 0;
    body.querySelectorAll('.bcard').forEach(function (c) {
      const hit = !q || (c.getAttribute('data-search') || '').indexOf(q) >= 0;
      c.hidden = !hit;
      if (hit) { visible += 1; }
    });
    const cc = document.getElementById('cc-' + k);
    if (cc) { cc.textContent = visible; }
    const empty = body.querySelector('.empty-col');
    if (visible === 0) {
      const text = q ? '没有匹配的 Issue' : EMPTY[k];
      if (!empty) { body.insertAdjacentHTML('beforeend', "<div class='empty-col'>" + text + "</div>"); }
      else { empty.textContent = text; }
    } else if (empty) {
      empty.remove();
    }
  });
  applyShowEmpty();
}

function applyShowEmpty() {
  COLS.forEach(function (k) {
    const col = document.querySelector(".col[data-col='" + k + "']");
    if (!col) { return; }
    let visible = 0;
    col.querySelectorAll('.bcard').forEach(function (c) { if (!c.hidden) { visible += 1; } });
    const empty = visible === 0;
    col.classList.toggle('is-empty', empty);
    col.hidden = !SHOW_EMPTY && empty;
  });
}

async function refreshIssues() {
  try {
    const res = await fetch('/api/issues');
    if (!res.ok) { return; }
    const data = await res.json();
    WRITABLE = data.writable === true;
    for (const k of COLS) {
      const body = document.getElementById('col-' + k);
      if (body) { body.innerHTML = data.cols[k] || ''; }
    }
    const health = document.getElementById('health');
    if (health) {
      health.textContent = '运行中 ' + (data.counts.running || 0) + ' · 更新于 ' + data.updated;
    }
    const all = document.getElementById('chipAll');
    if (all) { all.textContent = data.counts.total; }
    const sum = document.getElementById('sum');
    if (sum) { sum.textContent = data.summary || sum.textContent; }
    markSel();
    applyFilter();
    if (CUR) { refreshCard(); updateNoteBox(); }
  } catch (err) { }
}
setInterval(refreshIssues, 15000);

document.getElementById('q').addEventListener('input', applyFilter);
document.getElementById('showEmpty').addEventListener('change', function (e) {
  SHOW_EMPTY = e.target.checked;
  localStorage.setItem('sb-show-empty', SHOW_EMPTY ? '1' : '0');
  applyShowEmpty();
});
document.addEventListener('keydown', function (e) {
  if (e.key === '/' && !(e.target instanceof HTMLInputElement) && !(e.target instanceof HTMLTextAreaElement)) {
    e.preventDefault();
    document.getElementById('q').focus();
  }
});
applyFilter();

const initialParams = new URLSearchParams(location.search);
const fromUrl = initialParams.get('issue');
const initialView = initialParams.get('view');
if (initialView && initialView !== 'active') {
  Promise.resolve(switchView(initialView === 'completed' ? 'history' : initialView)).then(function () {
    if (fromUrl) { openIssue(fromUrl); }
  });
} else if (fromUrl) { openIssue(fromUrl); }
</script>
"""


def parse_ts(text):
    try:
        parsed = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def fmt_dt(text):
    parsed = parse_ts(text)
    return parsed.astimezone(CN).strftime("%Y-%m-%d %H:%M:%S") if parsed else str(text or "")


def fmt_clock(text):
    parsed = parse_ts(text)
    return parsed.astimezone(CN).strftime("%H:%M:%S") if parsed else ""


def read_records(path):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def session_header(records):
    return next((r for r in records if r.get("type") == "session"), {})


def issue_of(header):
    return os.path.basename((header.get("cwd") or "").rstrip("/")) or "unknown"


def ts_epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def count_messages(path, live):
    try:
        with open(path, "rb") as handle:
            if not live:
                return sum(1 for line in handle if line.startswith(b'{"type":"message"'))
            n = 0
            for line in handle:
                if b"entry_appended" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                entry = rec.get("entry") or {}
                if rec.get("type") == "entry_appended" and entry.get("type") == "message":
                    n += 1
            return n
    except OSError:
        return 0


def scan_sessions():
    now = time.time()
    if _cache["sessions"] and now - _cache["sessions_at"] < SESSION_CACHE_TTL:
        return _cache["sessions"]

    grouped = {}
    candidates = [(p, False) for p in glob.glob(os.path.join(SESS_DIR, "*.jsonl"))]
    candidates += [(p, True) for p in glob.glob(os.path.join(LIVE_DIR, "*.jsonl"))]
    for path, live in candidates:
        try:
            with open(path, "rb") as handle:
                header = json.loads(handle.readline().decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        if header.get("type") != "session":
            continue
        issue = issue_of(header)
        grouped.setdefault(issue, []).append(
            {
                "path": path,
                "live": live,
                "id": header.get("id") or "",
                "short": (header.get("id") or "")[:8],
                "started": fmt_dt(header.get("timestamp")),
                "started_ts": ts_epoch(header.get("timestamp")) or os.path.getmtime(path),
                "mtime": os.path.getmtime(path),
                "size": os.path.getsize(path),
                "messages": count_messages(path, live),
            }
        )

    for entries in grouped.values():
        pis = [r for r in entries if not r["live"]]
        lives = [r for r in entries if r["live"]]
        used = set()
        for lv in lives:
            best = None
            for i, p in enumerate(pis):
                if i in used:
                    continue
                dist = abs(lv["started_ts"] - p["started_ts"])
                if lv["id"] and lv["id"] == p["id"]:
                    score = (0, dist)
                elif dist <= 120:
                    score = (1, dist)
                else:
                    continue
                if best is None or score < best[0]:
                    best = (score, i, p)
            if best:
                used.add(best[1])
                lv["paired"] = True
                lv["_pi_idx"] = best[1]
                lv["short"] = best[2]["short"]
                lv["id"] = best[2]["id"]
                lv["messages"] = max(lv["messages"], best[2]["messages"])
        merged = []
        for lv in lives:
            if lv.get("paired") and now - lv["mtime"] > LIVE_RETENTION_S:
                try:
                    os.remove(lv["path"])
                except OSError:
                    merged.append(lv)
                    continue
                used.discard(lv["_pi_idx"])
                continue
            merged.append(lv)
        merged += [p for i, p in enumerate(pis) if i not in used]
        merged.sort(key=lambda r: r["mtime"])
        entries[:] = merged

    _cache["sessions"] = grouped
    _cache["sessions_at"] = now
    return grouped


def load_tracker():
    repo = None
    token = None
    try:
        with open(WORKFLOW, encoding="utf-8") as handle:
            match = REPO_RE.search(handle.read())
            if match:
                repo = match.group(1)
    except OSError:
        pass
    try:
        with open(ENV_FILE, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    return repo, token


PAUSE_COMMENT_PREFIX = "[agent] Paused"


def pause_reason(repo, token, number):
    """Latest host pause comment for an issue, best effort."""
    if not repo or not token:
        return ""
    try:
        comments = github_request(
            "GET", "/repos/%s/issues/%s/comments?per_page=30" % (repo, number), token
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, http.client.HTTPException):
        return ""
    if not isinstance(comments, list):
        return ""
    for comment in reversed(comments):
        body = str((comment or {}).get("body") or "")
        if body.startswith(PAUSE_COMMENT_PREFIX):
            reason = body[len(PAUSE_COMMENT_PREFIX):].lstrip()
            reason = reason.split("Delivery:", 1)[0].strip(" \n-：:")
            return reason[:600]
    return ""


def github_issues():
    now = time.time()
    if _cache["issues"] is not None and now - _cache["issues_at"] < ISSUE_CACHE_TTL:
        return _cache["issues"]

    repo, token = load_tracker()
    if not repo or not token:
        return _cache["issues"]

    request = urllib.request.Request(
        "https://api.github.com/repos/%s/issues?state=open&per_page=100" % repo,
        headers={
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "symphony-runs",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return _cache["issues"]

    issues = [
        i
        for i in payload
        if "pull_request" not in i
        and (i.get("author_association") or "").upper() in TRUSTED_ASSOCIATIONS
    ]
    _cache["issues"] = issues
    _cache["issues_at"] = now
    return issues


_pulls_cache = {"data": None, "at": 0.0}


def issue_number_from_pr(pr):
    ref = (pr.get("head") or {}).get("ref") or ""
    match = re.search(r"agent/issue-(\d+)", ref)
    if match:
        return int(match.group(1))
    body = pr.get("body") or ""
    match = re.search(r"(?:Closes|closes|Fixes|fixes|Resolves|resolves)\s+#(\d+)", body)
    if match:
        return int(match.group(1))
    return None


def github_pulls():
    now = time.time()
    if _pulls_cache["data"] is not None and now - _pulls_cache["at"] < ISSUE_CACHE_TTL:
        return _pulls_cache["data"]

    repo, token = load_tracker()
    if not repo or not token:
        return _pulls_cache["data"] or {}

    request = urllib.request.Request(
        "https://api.github.com/repos/%s/pulls?state=open&per_page=100" % repo,
        headers={
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "symphony-runs",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return _pulls_cache["data"] or {}

    pulls = {}
    for pr in payload:
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        if head_repo != repo:
            continue
        number = issue_number_from_pr(pr)
        if number is None:
            continue
        pulls[number] = {
            "number": pr.get("number"),
            "url": pr.get("html_url") or "",
            "draft": bool(pr.get("draft")),
            "title": pr.get("title") or "",
        }
    _pulls_cache["data"] = pulls
    _pulls_cache["at"] = now
    return pulls


def github_request(method, path, token, body=None, timeout=10):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=data,
        headers={
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "symphony-runs",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


def fetch_issue(repo, token, number):
    try:
        return github_request("GET", "/repos/%s/issues/%s" % (repo, number), token)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None


def issue_trusted(issue):
    return (issue.get("author_association") or "").upper() in TRUSTED_ASSOCIATIONS


def clear_github_cache():
    _cache["issues"] = None
    _cache["issues_at"] = 0.0
    _pulls_cache["data"] = None
    _pulls_cache["at"] = 0.0


def symphony_state():
    try:
        with urllib.request.urlopen(STATE_URL, timeout=3) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None


def agent_status(state):
    if not state:
        return {}
    flags = {}
    for bucket, label in (("running", "running"), ("blocked", "blocked"), ("retrying", "retrying")):
        for entry in state.get(bucket) or []:
            name = entry.get("issue_identifier")
            if name:
                flags[name] = label
    return flags


def agent_detail(state):
    if not state:
        return {}
    entries = {}
    for entry in state.get("running") or []:
        name = entry.get("issue_identifier")
        if name:
            entries[name] = entry
    return entries


def agent_errors(state):
    if not state:
        return {}
    errors = {}
    for bucket in ("blocked", "retrying"):
        for entry in state.get(bucket) or []:
            name = entry.get("issue_identifier")
            if name:
                errors[name] = {
                    "bucket": bucket,
                    "attempt": entry.get("attempt") or 0,
                    "error": entry.get("error") or "",
                    "short": short_error(entry.get("error") or ""),
                    "due_at": entry.get("due_at") or "",
                }
    return errors


def short_error(text):
    text = (text or "").strip()
    match = re.search(r"\{:([a-z_]+),(.*?)\}", text, re.S)
    if match:
        detail = match.group(2).strip().strip('"').replace('\\"', '"')
        detail = re.sub(r"\s+", " ", detail)
        return ("%s %s" % (match.group(1), detail))[:140]
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= 140 else text[:137] + "…"


def esc(text):
    return html.escape(str(text) if text is not None else "")


def human_size(size):
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return "%d%s" % (size, unit) if unit == "B" else "%.1f%s" % (size, unit)
        size /= 1024.0
    return "%.1fG" % size


def page(title, body, refresh=None):
    meta = "<meta http-equiv='refresh' content='%d'>" % refresh if refresh else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>%s"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>%s</title><style>%s</style></head><body>%s</body></html>"
        % (meta, esc(title), STYLE, body)
    )


STATUS_UI = {
    "running": ("运行中", "running"),
    "blocked": ("阻塞", "blocked"),
    "retrying": ("重试中", "retrying"),
    "ready": ("待处理", "ready"),
    "awaiting": ("待合并", "awaiting"),
    "idle": ("空闲", "idle"),
}

COLUMNS = [
    ("running", "运行中", "执行中 · 运行状态只在本机记录"),
    ("blocked", "阻塞", "失败或需要人工处理"),
    ("retrying", "重试中", "等待下一次重试"),
    ("ready", "待处理", "等待调度器领取 · agent 标签"),
    ("awaiting", "待合并", "PR 已开 · 等待人工 review / 合并"),
    ("idle", "空闲", "既无 agent 标签也无 open PR"),
]

STATUS_TEXT = {key: value[0] for key, value in STATUS_UI.items()}

EMPTY_TEXT = {
    "running": "没有正在执行的 Issue",
    "blocked": "没有异常状态",
    "retrying": "没有等待重试的 Issue",
    "ready": "没有等待派发的 Issue",
    "awaiting": "没有等待合并的 PR",
    "idle": "没有空闲的 Issue",
}

ACTIVITY_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>'
)

REFRESH_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/>'
    '<path d="M21 3v6h-6"/></svg>'
)

SEARCH_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>'
)

ICONS = {
    "running": (
        '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6" opacity=".35"/><circle class="spin" '
        'cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" stroke-dasharray="26 35.2" transform="rotate(-90 8 8)" '
        'style="transform-origin:8px 8px"/></svg>'
    ),
    "blocked": (
        '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6"/><path d="M8 4.9v3.6" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>'
        '<circle cx="8" cy="10.9" r=".95" fill="currentColor"/></svg>'
    ),
    "retrying": (
        '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        'stroke-dasharray="9.5 7.5"/></svg>'
    ),
    "ready": (
        '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6"/></svg>'
    ),
    "awaiting": (
        '<svg viewBox="0 0 16 16"><circle cx="4.6" cy="4.2" r="1.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6"/><circle cx="4.6" cy="11.8" r="1.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6"/><circle cx="11.4" cy="8" r="1.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6"/><path d="M4.6 5.8v4.4M6.2 8h3.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>'
    ),
    "idle": (
        '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" '
        'stroke="currentColor" stroke-width="1.4" opacity=".55"/></svg>'
    ),
}

EXT_ICON = (
    '<svg viewBox="0 0 16 16"><path fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round" d="M6.5 3.5h-3v9h9v-3M9.5 3.5h3v3M12 4 8 8"/></svg>'
)


def status_of(issue, labels, agent, has_pr=False):
    if agent in ("running", "blocked", "retrying"):
        return agent
    lower = {label.lower() for label in labels}
    # needs-human pauses execution and conflicts with the execution label.
    if "needs-human" in lower:
        return "blocked"
    if "agent" in lower:
        return "ready"
    if has_pr:
        return "awaiting"
    return "idle"


def build_cards():
    repo, token = load_tracker()
    issues = github_issues()
    if issues is None:
        return None
    sessions = scan_sessions()
    state = symphony_state()
    status = agent_status(state)
    agents = agent_detail(state)
    errors = agent_errors(state)
    pulls = github_pulls()

    rendered = []
    counts = {"running": 0, "blocked": 0, "retrying": 0, "ready": 0, "awaiting": 0, "idle": 0}

    for issue in issues:
        number = issue.get("number")
        name = "%s%d" % (IDENTIFIER_PREFIX, number)
        entries = sessions.get(name) or []
        labels = [l.get("name", "") for l in issue.get("labels") or []]
        agent = status.get(name)
        pull = pulls.get(number)
        st = status_of(name, labels, agent, pull is not None)
        counts[st] = counts.get(st, 0) + 1

        detail = agents.get(name) or {}
        trouble = errors.get(name) or {}
        since = detail.get("started_at") if st == "running" else ""
        runs = len(entries)
        latest = entries[-1]["started"] if entries else ""
        if since:
            right = "<span class='bright'><span data-since='%s'>已运行</span></span>" % esc(since)
        elif st in ("retrying", "blocked") and trouble:
            right = "<span class='bright'>第 %d 次重试</span>" % (trouble.get("attempt") or 0)
        elif runs:
            right = "<span class='bright'>%d 次运行 · %s</span>" % (runs, esc(latest[5:16]))
        else:
            right = "<span class='bright'>未运行</span>"

        err_html = ""
        if trouble.get("error"):
            full = trouble["error"]
            err_html = "<div class='berr' title='%s'>%s</div>" % (
                esc(full),
                esc(short_error(full)),
            )

        title = issue.get("title") or ""
        gen = "<span class='bgen'>%d 次</span>" % runs if runs else ""
        has_agent = any(label.lower() == "agent" for label in labels)
        has_paused = any(label.lower() == "needs-human" for label in labels)
        pause_note = pause_reason(repo, token, number) if has_paused else ""
        state_text = "等待人工处理" if has_paused else STATUS_TEXT[st]
        pr_html = ""
        pr_url = ""
        pr_number = ""
        if pull:
            pr_url = pull.get("url") or ""
            pr_number = str(pull.get("number") or "")
            pr_html = "<a class='bpr' href='%s' target='_blank' rel='noopener noreferrer' title='%s'>#%s%s</a>" % (
                esc(pr_url),
                esc(pull.get("title") or ""),
                esc(pr_number),
                " 草稿" if pull.get("draft") else "",
            )
        search = ("%s %s %s" % (name, title, repo or "")).lower()
        rendered.append(
            {
                "state": st,
                "runs": runs,
                "updated": issue.get("updated_at") or "",
                "html": (
                    "<div class='bcard %(state)s%(live)s' data-issue='%(name)s' "
                    "data-title='%(title)s' data-url='%(url)s' data-state='%(state)s' "
                    "data-state-text='%(text)s' data-agent='%(agent)s' "
                    "data-paused='%(paused)s' data-pause-reason='%(pause)s' "
                    "data-pr='%(prurl)s' data-pr-num='%(prnum)s' data-search='%(search)s'>"
                    "<div class='btitle'>%(title)s</div>"
                    "<div class='bmeta'>"
                    "<div class='brow'><span class='bid'>%(name)s</span>%(gen)s%(pr)s"
                    "<a class='ext' href='%(url)s' target='_blank' rel='noopener noreferrer' "
                    "title='在 GitHub 打开'>%(ext)s</a></div>"
                    "<div class='brow'><span class='bstat tone-%(state)s'>%(icon)s%(text)s</span>"
                    "%(right)s</div>"
                    "</div>%(err)s</div>"
                )
                % {
                    "state": esc(st),
                    "live": " live" if st == "running" else "",
                    "name": esc(name),
                    "title": esc(title),
                    "url": esc(issue.get("html_url")),
                    "text": esc(state_text),
                    "agent": "1" if has_agent else "0",
                    "paused": "1" if has_paused else "0",
                    "pause": esc(pause_note),
                    "prurl": esc(pr_url),
                    "prnum": esc(pr_number),
                    "search": esc(search),
                    "gen": gen,
                    "ext": EXT_ICON,
                    "icon": ICONS[st],
                    "pr": pr_html,
                    "right": right,
                    "err": err_html,
                },
            }
        )

    rendered.sort(key=lambda r: r["updated"], reverse=True)
    with_runs = sum(1 for e in rendered if e["runs"])
    cols = {}
    for key in STATUS_UI:
        cols[key] = "".join(r["html"] for r in rendered if r["state"] == key)
    return {
        "cols": cols,
        "counts": counts,
        "total": len(rendered),
        "with_runs": with_runs,
        "repo": repo or "-",
        "writable": bool(repo and token),
    }


def issues_page():
    data = build_cards()
    if data is None:
        sessions = scan_sessions()
        cards = []
        for name in sorted(sessions, key=lambda n: sessions[n][-1]["mtime"], reverse=True):
            entries = sessions[name]
            cards.append(
                "<a class='irow' href='/issue/%s'>"
                "<span class='st idle'>%s</span>"
                "<div class='ibody'><div class='ihead'>"
                "<span class='iid'>%s</span><span class='title'>%s</span></div>"
                "<div class='isub'>%d 次运行</div></div>"
                "<span class='badge idle'>仅记录</span></a>"
                % (esc(name), ICONS["idle"], esc(name), esc(name), len(entries))
            )
        body = (
            "<div class='app'><header class='topbar'><div class='brand'>%s<span>Symphony</span></div>"
            "<div class='health'><span class='dot'></span>"
            "<span>GitHub 不可用 · 仅显示本地记录</span></div></header>"
            "<div class='head'><div class='hrow'><h1>Issue 工作台</h1>"
            "<span class='sum'>%d 个有记录的 issue</span></div>"
            "<div class='note'>GitHub issues unavailable; showing recorded runs only · <a href='/history'>今日完成 / 历史记录</a></div></div>"
            "<div class='tools'><div class='chips'><span class='chip on'>全部 <b>%d</b></span></div></div>"
            "<main class='board' id='board'>%s</main></div>"
            % (ACTIVITY_ICON, len(sessions), len(sessions),
               "".join(cards) or "<div class='empty-col'>no runs recorded yet</div>")
        )
        return page("Symphony", body)

    counts = data["counts"]
    summary = " · ".join(
        "%d %s" % (counts.get(key, 0), label)
        for key, label, _sub in COLUMNS
        if counts.get(key, 0)
    ) or "当前没有任务"
    col_html = []
    for key, label, sub in COLUMNS:
        cards = data["cols"].get(key) or ""
        inner = cards or "<div class='empty-col'>%s</div>" % EMPTY_TEXT[key]
        col_html.append(
            "<section class='col%s' data-col='%s'>"
            "<header class='col-head'><span class='cdot %s'></span>"
            "<div class='chead'><div class='cline'><span class='clabel'>%s</span>"
            "<span class='ccount' id='cc-%s'>%d</span></div>"
            "<div class='csub'>%s</div></div></header>"
            "<div class='col-body' id='col-%s'>%s</div></section>"
            % ("" if cards else " is-empty", key, key, label, key, counts.get(key, 0), sub, key, inner)
        )

    repo = data["repo"]
    has_repo = "/" in repo
    repo_url = "https://github.com/%s" % repo if has_repo else "#"
    repo_chip = (
        "<button class='chip' data-repo='%s'>%s <b>%d</b></button>"
        % (esc(repo), esc(repo), data["total"])
        if has_repo else ""
    )
    shell = (
        "<div class='app'>"
        "<header class='topbar'>"
        "<div class='brand'>%s<span>Symphony</span></div>"
        "<div class='health'><span class='dot'></span>"
        "<span id='health'>运行中 %d · 更新于 %s</span></div>"
        "<div class='auto'>%s<span>自动刷新 15 秒</span>"
        "<a href='%s' target='_blank' rel='noopener noreferrer'>%s</a></div>"
        "</header>"
        "<div class='head'><div class='hrow'><h1>Issue 工作台</h1>"
        "<span class='sum' id='sum'>%s</span></div>"
        "<div class='note'>业务状态来自 Symphony 的 agent / needs-human 标签与本地运行状态（最近一次对账）；"
        "点击卡片可查看历次运行、实时日志，并直接操作 agent / needs-human 标签（Running/Retrying 等执行状态只记录在本机）。</div></div>"
        "<div class='tools'>"
        "<div class='chips'><button class='chip on' data-repo=''>全部 "
        "<b id='chipAll'>%d</b></button>%s</div>"
        "<div class='view-tabs' role='group' aria-label='Issue 视图'>"
        "<button class='chip on' data-view='active' aria-pressed='true'>处理中</button>"
        "<button class='chip' data-view='today' aria-pressed='false'>今日已完成</button>"
        "<button class='chip' data-view='history' aria-pressed='false'>历史已完成</button></div>"
        "<label class='chk'><input type='checkbox' id='showEmpty' checked>显示空列</label>"
        "<div class='search'>%s<input id='q' placeholder='搜索标题、仓库或 #编号' autocomplete='off'></div>"
        "</div>"
        "<main class='board' id='board' data-writable='%s'>%s</main>"
        "<main class='completed-panel' id='completed-panel' hidden>"
        "<div class='completed-toolbar'><b id='completed-count'></b>"
        "<button class='chip' id='completed-refresh'>刷新</button></div>"
        "<p class='completed-note'>仅展示已关闭且未标记为“放弃处理”的 Issue，不包含待审核交付、暂停或失败记录。点击卡片在当前页查看。</p>"
        "<p id='completed-status' role='status' aria-live='polite'></p>"
        "<div class='completed-list' id='completed-list'></div></main>"
        "</div>"
        "<div id='shade' onclick='closeDrawer()'></div>"
        "<div id='toast'></div>"
        "<aside class='drawer' id='drawer'><div class='dhead' id='dhead'></div>"
        "<div class='dbody' id='dbody'></div></aside>"
    ) % (
        ACTIVITY_ICON,
        counts.get("running", 0),
        datetime.now(CN).strftime("%H:%M:%S"),
        REFRESH_ICON,
        esc(repo_url),
        esc(repo),
        summary,
        data["total"],
        repo_chip,
        SEARCH_ICON,
        "1" if data.get("writable") else "0",
        "".join(col_html),
    )
    return page(
        "Symphony",
        shell + BOARD,
    )


_history_cache = {"repo": None, "at": 0.0, "events": None}


def github_history():
    """Completed issues, not workflow attempts or agent-session endings."""
    repo, token = load_tracker()
    if not repo or not token:
        return [], "GitHub 未配置，无法读取已完成 Issue。"
    if (_history_cache["repo"] == repo and _history_cache["events"] is not None
            and time.time() - _history_cache["at"] < ISSUE_CACHE_TTL):
        return _history_cache["events"], _history_cache.get("warning", "")
    events = []
    limited = False
    try:
        for page_number in range(1, 11):
            rows = github_request(
                "GET", "/repos/%s/issues?state=closed&sort=updated&direction=desc&per_page=100&page=%d"
                % (repo, page_number), token
            )
            if not isinstance(rows, list):
                raise ValueError("invalid GitHub history response")
            for row in rows:
                if "pull_request" in row or row.get("state_reason") == "not_planned":
                    continue
                if row.get("state") != "closed" or not row.get("closed_at"):
                    continue
                events.append({
                    "number": row["number"], "title": row.get("title") or "",
                    "at": row["closed_at"], "url": row.get("html_url") or "",
                    "body": (row.get("body") or "")[:16000],
                })
            if len(rows) < 100:
                break
        else:
            limited = True
        events.sort(key=lambda event: event["at"], reverse=True)
        warning = "仅扫描最近更新的 1000 条已关闭条目（含 PR）；更早记录请到 GitHub 查看。" if limited else ""
        _history_cache.update(repo=repo, at=time.time(), events=events, warning=warning)
        return events, warning
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        old = _history_cache["events"] if _history_cache["repo"] == repo else None
        return old or [], "GitHub 已完成记录读取失败；显示上次缓存（如有），请稍后重试。"


def completed_data():
    events, warning = github_history()
    today = datetime.now(CN).date().isoformat()
    issues = []
    for event in events:
        timestamp = parse_ts(event["at"])
        if timestamp is None:
            continue
        issues.append({**event, "day": timestamp.astimezone(CN).date().isoformat(),
                       "completed_at": fmt_dt(event["at"])})
    return {"issues": issues, "today": today, "warning": warning}


def issue_page(name):
    sessions = scan_sessions()
    entries = sessions.get(name) or []
    state = status_of(name, [], agent_status(symphony_state()).get(name, "idle"))
    label_text, badge_cls = STATUS_UI[state]

    header = (
        "<h1>%s <span class='badge %s'>%s</span></h1>"
        "<div class='meta'><a href='/'>返回全部 issue</a> &middot; "
        "<a href='/live/%s'>实时视图</a> &middot; %d 次运行</div>"
        % (esc(name), esc(badge_cls), esc(label_text), esc(name), len(entries))
    )

    if not entries:
        return page(name, header + "<div class='empty'>该 issue 还没有运行记录</div>")

    cards = []
    for position, run in enumerate(reversed(entries), 1):
        href = "/run/%s/%s" % (name, run["short"])
        latest = "<span class='badge ready'>最新</span>" if position == 1 else ""
        cards.append(
            "<div class='card state-idle'>"
            "<div class='cardhead'><span class='ico idle'>%s</span>"
            "<a class='title' href='%s'>%s</a>%s</div>"
            "<div class='sub'>%s &middot; %d 条消息 &middot; %s</div>"
            "<div class='right'><span class='count'>%s</span></div></div>"
            % (
                ICONS["idle"],
                esc(href),
                esc(run["started"]),
                latest,
                human_size(run["size"]),
                run["messages"],
                esc(run["short"]),
                human_size(run["size"]),
            )
        )

    return page(name, header + "".join(cards))


def run_page(issue, short):
    entries = scan_sessions().get(issue) or []
    run = next((r for r in entries if r["short"].startswith(short)), None)
    if not run:
        return None

    with open(run["path"], "rb") as handle:
        raw = handle.read(HEAD_BYTES)
    truncated = os.path.getsize(run["path"]) > len(raw)
    end = raw.rfind(b"\n")
    if end >= 0:
        raw = raw[: end + 1]

    fragments = []
    if run.get("live"):
        st = {"offset": 0, "blocks": {}, "seq": 0, "tools": {},
              "open_msg": None, "last_tool": None, "ts": 0}
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                _, h = live_feed(st, json.loads(line))
            except ValueError:
                continue
            fragments.append(h)
    else:
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                fragments.append(render_record(json.loads(line)))
            except ValueError:
                continue

    header = (
        "<h1>%s · %s</h1>"
        "<div class='meta'>%s · %s · %d messages · "
        "<a href='/issue/%s'>all runs</a> · <a href='/live/%s'>live</a></div>"
        % (
            esc(issue),
            esc(run["started"]),
            esc(run["short"]),
            human_size(run["size"]),
            run["messages"],
            esc(issue),
            esc(issue),
        )
    )
    note = (
        "<div class='meta warn'>showing the first %s only; use the archive or the CLI for the rest</div>"
        % human_size(HEAD_BYTES)
        if truncated
        else ""
    )
    return page("%s %s" % (issue, run["started"]), header + note + "".join(fragments))


def render_record(record):
    if record.get("type") != "message":
        return ""
    message = record.get("message", {})
    role = message.get("role")
    if role == "system":
        return ""
    stamp = fmt_clock(record.get("timestamp"))
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
    lines = []

    if role == "user":
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if text:
            lines.append("<div class='ln mark'>[%s] 用户</div>" % stamp)
            lines.append("<div class='ln user'>%s</div>" % esc(clip(text, TEXT_LIMIT)))
    elif role == "toolResult":
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if text:
            lines.append(
                "<div class='ln tool'><div class='out'>%s</div></div>"
                % esc(clip(text, TEXT_LIMIT))
            )
    else:
        for part in blocks:
            kind = part.get("type")
            if kind == "text":
                lines.append("<div class='ln say'>%s</div>" % esc(clip(part.get("text", ""), TEXT_LIMIT)))
            elif kind == "thinking":
                text = clip(part.get("thinking", ""), THINK_LIMIT)
                lines.append(
                    "<div class='ln think'><span class='th'>思考 · %d 行</span>%s</div>"
                    % (text.count("\n") + 1, esc(text))
                )
            elif kind == "toolCall":
                args = json.dumps(part.get("arguments", {}), ensure_ascii=False)
                lines.append(
                    "<div class='ln tool'><div class='th'>▶ %s %s</div></div>"
                    % (esc(part.get("name")), esc(clip(args, THINK_LIMIT)))
                )
        usage = message.get("usage") or {}
        if role == "assistant" and usage:
            lines.append(
                "<div class='ln tokens'>tokens in=%s out=%s</div>"
                % (usage.get("input"), usage.get("output"))
            )

    if not lines:
        return ""
    return "<div class='msg'>%s</div>" % "".join(lines)


def clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "\n... (truncated)"


def tail_chunk(path, offset):
    try:
        size = os.path.getsize(path)
    except OSError:
        return {
            "offset": offset or 0,
            "html": "",
            "pending": 0,
            "truncated": False,
            "start": offset or 0,
            "size": 0,
        }

    first = offset is None
    start = max(0, size - MAX_CHUNK) if first else min(offset, size)

    with open(path, "rb") as handle:
        handle.seek(start)
        raw = handle.read(max(0, min(MAX_CHUNK, size - start)))

    if first and start > 0:
        cut = raw.find(b"\n")
        if cut < 0:
            return {
                "offset": size,
                "html": "",
                "pending": 0,
                "truncated": False,
                "start": start,
                "size": size,
            }
        raw = raw[cut + 1 :]
        start += cut + 1

    end = raw.rfind(b"\n")
    if end < 0:
        return {
            "offset": start,
            "html": "",
            "pending": len(raw),
            "truncated": False,
            "start": start,
            "size": size,
        }

    complete = raw[: end + 1].decode("utf-8", "replace")
    pending = len(raw) - end - 1
    fragments = []
    for line in complete.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            fragments.append(render_record(json.loads(line)))
        except ValueError:
            continue

    return {
        "offset": start + end + 1,
        "html": "".join(fragments),
        "pending": pending,
        "truncated": first and start > 0,
        "start": start,
        "size": size,
    }


_live_state = {}


def result_text(result):
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            return "\n".join(
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, list):
        return "\n".join(
            result_text(p) for p in result
        )
    return ""


def tool_args_text(record):
    name = record.get("toolName") or "tool"
    args = record.get("args")
    if name == "bash" and isinstance(args, dict) and args.get("command"):
        return "$ " + str(args["command"])
    try:
        return "%s %s" % (name, json.dumps(args or {}, ensure_ascii=False))
    except (TypeError, ValueError):
        return name


def live_block_html(bid, block):
    kind = block.get("kind")
    if kind == "entry":
        inner = render_record(block.get("record") or {})
        if inner.startswith("<div "):
            inner = inner.replace("<div ", "<div data-bid='%s' " % esc(bid), 1)
        return inner
    if kind == "meta":
        return (
            "<div class='msg' data-bid='%s'><div class='ln mark err'>%s</div></div>"
            % (esc(bid), block.get("text", ""))
        )
    if kind == "tool":
        err = block.get("err")
        state = "" if block.get("done") else " · 执行中"
        tm = ("✗ " if err else "✓ ") + esc(block.get("time", "")) + state
        out = block.get("out") or ""
        return (
            "<div class='msg' data-bid='%s'><div class='ln tool%s'>"
            "<div class='th'>▶ %s</div>%s<div class='tm'>%s</div></div></div>"
            % (
                esc(bid),
                " err" if err else "",
                esc(block.get("args_text", "")),
                ("<div class='out'>%s</div>" % esc(clip(out, TEXT_LIMIT))) if out else "",
                tm,
            )
        )
    parts = []
    for ci in block.get("order", []):
        part = block["parts"][ci]
        if part["k"] == "think":
            text = clip(part["text"], THINK_LIMIT)
            parts.append(
                "<div class='ln think'><span class='th'>思考 · %d 行</span>%s</div>"
                % (text.count("\n") + 1, esc(text))
            )
        elif part["k"] == "tool":
            parts.append(
                "<div class='ln tool'><div class='th'>▶ %s</div></div>"
                % esc(clip(part["text"], THINK_LIMIT))
            )
        else:
            parts.append("<div class='ln say'>%s</div>" % esc(clip(part["text"], TEXT_LIMIT)))
    usage = block.get("tokens") or {}
    if usage:
        parts.append(
            "<div class='ln tokens'>tokens in=%s out=%s</div>"
            % (usage.get("input"), usage.get("output"))
        )
    return "<div class='msg' data-bid='%s'>%s</div>" % (esc(bid), "".join(parts))


def live_feed(st, record):
    ops = []
    html_parts = []
    blocks = st["blocks"]
    now = datetime.now(CN).strftime("%H:%M:%S")

    def upsert(bid):
        ops.append({"op": "upsert", "id": bid, "html": live_block_html(bid, blocks[bid])})

    rtype = record.get("type")

    if rtype == "entry_appended":
        entry = record.get("entry") or {}
        if entry.get("type") == "message":
            if st.get("open_msg") and st["open_msg"] in blocks:
                ops.append({"op": "del", "id": st["open_msg"]})
                del blocks[st["open_msg"]]
                st["open_msg"] = None
            entry_msg = entry.get("message") or {}
            entry_ts = entry_msg.get("timestamp") or entry.get("timestamp")
            for bkey, block in list(blocks.items()):
                if (
                    bkey.startswith("u-")
                    and (block.get("record") or {}).get("timestamp") == entry_ts
                    and ((block.get("record") or {}).get("message") or {}).get("role")
                    == entry_msg.get("role")
                ):
                    ops.append({"op": "del", "id": bkey})
                    del blocks[bkey]
            bid = "e-" + str(entry.get("id") or st["seq"])
            st["seq"] += 1
            blocks[bid] = {"kind": "entry", "record": entry}
            html_parts.append(render_record(entry))
            upsert(bid)

    elif rtype == "tool_execution_start":
        tid = record.get("toolCallId")
        bid = "t-" + str(tid or st["seq"])
        st["seq"] += 1
        if tid is not None:
            st["tools"][tid] = bid
        st["last_tool"] = bid
        blocks[bid] = {
            "kind": "tool",
            "name": record.get("toolName") or "tool",
            "args_text": tool_args_text(record),
            "out": "",
            "err": False,
            "done": False,
            "time": now,
        }
        upsert(bid)

    elif rtype == "tool_execution_update":
        bid = st["tools"].get(record.get("toolCallId"))
        if bid and bid in blocks:
            text = result_text(record.get("partialResult"))
            if text:
                blocks[bid]["out"] = text
            upsert(bid)

    elif rtype == "tool_execution_end":
        tid = record.get("toolCallId")
        bid = st["tools"].pop(tid, None) if tid is not None else None
        bid = bid or ("t-" + str(tid) if tid is not None else st.get("last_tool"))
        if bid is None:
            bid = "t-" + str(st["seq"])
            st["seq"] += 1
        if bid not in blocks:
            blocks[bid] = {
                "kind": "tool",
                "name": record.get("toolName") or "tool",
                "args_text": "",
                "out": "",
                "err": False,
                "done": True,
                "time": now,
            }
        b = blocks[bid]
        b["name"] = record.get("toolName") or b.get("name")
        text = result_text(record.get("result"))
        if text:
            b["out"] = text
        b["err"] = record.get("isError") is True
        b["done"] = True
        upsert(bid)
        html_parts.append(live_block_html(bid, b))

    elif rtype == "bash_execution_update":
        tid = record.get("id")
        bid = st["tools"].get(tid) if tid is not None else None
        bid = bid or ("t-" + str(tid) if tid is not None and ("t-" + str(tid)) in blocks else st.get("last_tool"))
        if bid and bid in blocks:
            blocks[bid]["out"] += str(record.get("delta") or "")
            upsert(bid)

    elif rtype == "message_start":
        message = record.get("message") or {}
        role = message.get("role")
        if role == "assistant":
            st["seq"] += 1
            bid = "s-%d" % st["seq"]
            blocks[bid] = {
                "kind": "msg",
                "role": "assistant",
                "time": now,
                "parts": {},
                "order": [],
            }
            st["open_msg"] = bid
            upsert(bid)
        elif role not in ("system", "toolResult"):
            bid = "u-%d" % st["seq"]
            st["seq"] += 1
            blocks[bid] = {
                "kind": "entry",
                "record": {
                    "type": "message",
                    "timestamp": record.get("timestamp") or message.get("timestamp"),
                    "message": message,
                },
            }
            html_parts.append(render_record(blocks[bid]["record"]))
            upsert(bid)

    elif rtype == "message_update":
        bid = st.get("open_msg")
        ev = record.get("assistantMessageEvent") or {}
        et = ev.get("type")
        if bid is None and et in ("text_delta", "thinking_delta", "toolcall_end"):
            st["seq"] += 1
            bid = "s-%d" % st["seq"]
            blocks[bid] = {"kind": "msg", "role": "assistant", "time": now, "parts": {}, "order": []}
            st["open_msg"] = bid
        if bid and bid in blocks:
            b = blocks[bid]
            ci = ev.get("contentIndex")
            if et in ("text_start", "thinking_start") and ci is not None:
                if ci not in b["parts"]:
                    b["parts"][ci] = {"k": "think" if et == "thinking_start" else "text", "text": ""}
                    b["order"].append(ci)
            elif et in ("text_delta", "thinking_delta") and ci is not None:
                if ci not in b["parts"]:
                    b["parts"][ci] = {"k": "think" if et == "thinking_delta" else "text", "text": ""}
                    b["order"].append(ci)
                b["parts"][ci]["text"] += str(ev.get("delta") or "")
            elif et in ("text_end", "thinking_end") and ci is not None:
                if ci not in b["parts"]:
                    b["parts"][ci] = {"k": "think" if et == "thinking_end" else "text", "text": ""}
                    b["order"].append(ci)
                b["parts"][ci]["text"] = str(ev.get("content") or "")
            elif et == "toolcall_end":
                tc = ev.get("toolCall") or {}
                ci = ci if ci is not None else len(b["order"])
                args = json.dumps(tc.get("arguments") or {}, ensure_ascii=False)
                b["parts"][ci] = {"k": "tool", "text": "[tool] %s %s" % (tc.get("name"), args)}
                b["order"].append(ci)
            if et:
                upsert(bid)

    elif rtype == "message_end":
        bid = st.get("open_msg")
        if bid and bid in blocks:
            usage = (record.get("message") or {}).get("usage") or {}
            if usage:
                blocks[bid]["tokens"] = usage
                upsert(bid)

    elif rtype == "error":
        bid = "x-%d" % st["seq"]
        st["seq"] += 1
        blocks[bid] = {"kind": "meta", "text": "error: %s" % record.get("message", "")}
        upsert(bid)

    return ops, "".join(html_parts)


def tail_live(path, offset, src=""):
    now_ts = time.time()
    for key, val in list(_live_state.items()):
        if now_ts - val.get("ts", 0) > 600:
            del _live_state[key]

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
        path_missing = True
    else:
        path_missing = False

    if path_missing:
        return {"offset": 0, "start": 0, "size": 0, "ops": [], "html": "",
                "eof": True, "truncated": False, "pending": 0, "reset": False}

    key = (path, src)
    st = _live_state.get(key)
    if st is None:
        if offset:
            return {"offset": 0, "start": 0, "size": size, "ops": [], "html": "",
                    "eof": False, "truncated": False, "pending": 0, "reset": True}
        st = {"offset": 0, "blocks": {}, "seq": 0, "tools": {},
              "open_msg": None, "last_tool": None, "ts": now_ts}
        _live_state[key] = st
    st["ts"] = now_ts

    first = offset is None
    start = max(0, size - MAX_CHUNK) if first else min(offset, size)

    with open(path, "rb") as handle:
        handle.seek(start)
        raw = handle.read(max(0, min(MAX_CHUNK, size - start)))

    if first and start > 0:
        cut = raw.find(b"\n")
        if cut >= 0:
            raw = raw[cut + 1 :]
            start += cut + 1

    end = raw.rfind(b"\n")
    if end < 0:
        return {"offset": start, "start": start, "size": size, "ops": [], "html": "",
                "eof": False, "truncated": False, "pending": len(raw), "reset": False}

    complete = raw[: end + 1].decode("utf-8", "replace")
    pending = len(raw) - end - 1
    ops = []
    html_parts = []
    for line in complete.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        o, h = live_feed(st, record)
        ops.extend(o)
        html_parts.append(h)

    blocks = st["blocks"]
    if len(blocks) > 1200:
        for bkey in list(blocks)[: len(blocks) - 800]:
            del blocks[bkey]

    last_op = {}
    for i, op in enumerate(ops):
        last_op[op["id"]] = (i, op)
    ops = [op for _, op in sorted(last_op.values())]

    new_offset = start + end + 1
    st["offset"] = new_offset
    return {
        "offset": new_offset,
        "start": start,
        "size": size,
        "ops": ops,
        "html": "".join(html_parts),
        "eof": new_offset >= size,
        "truncated": first and start > 0,
        "pending": pending,
        "reset": False,
    }


def live_page(issue, entries):
    if not entries:
        return page(issue, "<h1>%s</h1><div class='meta'>no recorded runs yet</div>" % esc(issue))

    run = entries[-1]
    src = "live-" + ("%08x" % int(time.time() * 1000))[-8:] + ("%04x" % (os.getpid() % 65536))
    if run.get("live"):
        chunk = tail_live(run["path"], None, src)
    else:
        chunk = tail_chunk(run["path"], None)
    offset = chunk["offset"]
    fragment = chunk["html"]
    if chunk["truncated"]:
        fragment = "<div class='meta'>… 更早的记录已省略，仅显示最近部分 …</div>" + fragment
    alive = agent_detail(symphony_state()).get(issue) is not None
    state = "live" if alive or (time.time() - run["mtime"]) < LIVE_WINDOW else "idle"

    body = """
<h1>%(issue)s <span id='status' class='%(state)s'>%(label)s</span></h1>
<div class='meta'>%(started)s · %(short)s · <a href='/issue/%(issue)s'>runs</a> · <a href='/'>all issues</a><span id='trim'></span></div>
<div id='detail' class='meta'></div>
<div id='feed'>%(fragment)s</div>
<script>
const ISSUE = %(issue_json)s;
const SRC = %(src_json)s;
const MAX_MSGS = 400;
let offset = %(offset)d;
let AGENT = null;
let skipped = 0;
function trimFeed() {
  const feed = document.getElementById('feed');
  if (!feed) { return; }
  const msgs = feed.querySelectorAll('.msg');
  const excess = msgs.length - MAX_MSGS;
  for (let i = 0; i < excess; i++) { msgs[i].remove(); }
  if (excess > 0) {
    skipped += excess;
    const t = document.getElementById('trim');
    if (t) { t.textContent = ' · 顶部已省略 ' + skipped + ' 条'; }
  }
}
function ago(iso) {
  if (!iso) { return ""; }
  const s = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
  if (s < 60) { return s + "s"; }
  const m = Math.floor(s / 60);
  return m + "m" + (s %% 60) + "s";
}
function draw() {
  const d = document.getElementById('detail');
  if (!AGENT) { d.textContent = ""; return; }
  const doing = AGENT.message || AGENT.event || "";
  d.textContent = "▸ " + doing
    + (AGENT.event_at ? " · " + ago(AGENT.event_at) + "前" : "")
    + (AGENT.started_at ? " · 已运行 " + ago(AGENT.started_at) : "")
    + (AGENT.tokens ? " · tokens " + AGENT.tokens : "");
}
async function tick() {
  try {
    const res = await fetch('/api/tail/' + encodeURIComponent(ISSUE) + '?offset=' + offset + '&src=' + SRC);
    if (!res.ok) { return; }
    const data = await res.json();
    if (data.reset) { location.reload(); return; }
    offset = data.offset;
    AGENT = data.agent || null;
    if (data.html) {
      const feed = document.getElementById('feed');
      const pinned = window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
      feed.insertAdjacentHTML('beforeend', data.html);
      trimFeed();
      if (pinned) window.scrollTo(0, document.body.scrollHeight);
    }
    const el = document.getElementById('status');
    const live = data.running || !!AGENT;
    el.textContent = live ? 'live' : 'finished';
    el.className = live ? 'live' : 'idle';
    draw();
  } catch (err) { }
}
setInterval(tick, 1000);
setInterval(draw, 1000);
</script>
""" % {
        "issue": esc(issue),
        "state": state,
        "label": "live" if state == "live" else "idle",
        "started": esc(run["started"]),
        "short": esc(run["short"]),
        "fragment": fragment,
        "issue_json": json.dumps(issue),
        "src_json": json.dumps(src),
        "offset": offset,
    }
    return page("live " + issue, body)


class Handler(BaseHTTPRequestHandler):
    server_version = "symphony-board"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        try:
            self.route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - a board render must never kill the connection
            try:
                self.send_json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            except OSError:
                pass

    def route_get(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path.startswith("/api/tail/"):
            return self.handle_tail(path[len("/api/tail/") :], query)

        if path == "/history":
            self.send_response(302)
            self.send_header("Location", "/?view=completed&scope=all")
            self.end_headers()
            return

        if path == "/api/completed":
            return self.send_json(200, completed_data())

        if path == "/api/issues":
            return self.handle_issues()

        if path.startswith("/api/runs/"):
            return self.handle_runs(path[len("/api/runs/") :])

        if path.startswith("/api/run/"):
            rest = path[len("/api/run/") :].strip("/").split("/")
            if len(rest) != 2:
                return self.send_json(400, {"error": "bad run reference"})
            return self.handle_run_detail(rest[0], rest[1], query)

        if path.startswith("/live/"):
            issue = path[len("/live/") :].strip("/")
            if not ISSUE_RE.match(issue or ""):
                return self.send_text(400, "bad issue name")
            return self.send_html(200, live_page(issue, scan_sessions().get(issue) or []))

        if path.startswith("/issue/"):
            issue = path[len("/issue/") :].strip("/")
            if not ISSUE_RE.match(issue or ""):
                return self.send_text(400, "bad issue name")
            return self.send_html(200, issue_page(issue))

        if path.startswith("/run/"):
            rest = path[len("/run/") :].strip("/").split("/")
            if len(rest) != 2 or not ISSUE_RE.match(rest[0]) or not ISSUE_RE.match(rest[1]):
                return self.send_text(400, "bad run reference")
            body = run_page(rest[0], rest[1])
            if body is None:
                return self.send_text(404, "no such run")
            return self.send_html(200, body)

        if path.startswith("/archive/"):
            return self.serve_static(path[len("/archive/") :])

        if path in ("/", "/index.html"):
            return self.send_html(200, issues_page())

        return self.send_text(404, "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/label":
            return self.handle_label()
        if parsed.path == "/api/approve":
            return self.handle_approve()
        if parsed.path == "/api/intervene":
            return self.handle_intervene()
        return self.send_json(404, {"error": "not found"})

    def write_guard(self):
        origin = self.headers.get("Origin") or ""
        if origin and urlparse(origin).netloc != (self.headers.get("Host") or ""):
            self.send_json(403, {"error": "拒绝跨站请求"})
            return False
        site = self.headers.get("Sec-Fetch-Site") or ""
        if site and site not in ("same-origin", "none"):
            self.send_json(403, {"error": "拒绝跨站请求"})
            return False
        return True

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            self.send_json(400, {"error": "请求体为空或过大"})
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_json(400, {"error": "请求体不是合法 JSON"})
            return None

    def issue_number(self, value):
        issue = str(value or "")
        if not ISSUE_RE.match(issue) or not issue.startswith(IDENTIFIER_PREFIX):
            return None
        number_text = issue[len(IDENTIFIER_PREFIX):]
        return int(number_text) if number_text.isdigit() else None

    def handle_approve(self):
        """merge-approved 已从协议移除：合并由宿主自动判定，人工入口是 needs-human。"""
        if not self.write_guard():
            return
        return self.send_json(
            410,
            {"error": "merge-approved 已移除：合并由宿主自动判定；如需人工控制，用“转人工处理”（needs-human）或在 GitHub 摘掉 agent"},
        )

    def handle_label(self):
        """加 / 摘 agent 或 needs-human 标签，可选先发一条 issue 评论。"""
        if not self.write_guard():
            return
        payload = self.read_json_body()
        if payload is None:
            return
        action = payload.get("action")
        label = str(payload.get("label") or "agent").lower()
        comment = str(payload.get("comment") or "").strip()
        if label not in ("agent", "needs-human"):
            return self.send_json(400, {"error": "只允许修改 agent / needs-human 标签"})
        if action not in ("add", "remove"):
            return self.send_json(400, {"error": "action 必须是 add 或 remove"})
        if len(comment) > 4000:
            return self.send_json(400, {"error": "说明过长（上限 4000 字）"})
        number = self.issue_number(payload.get("issue"))
        if number is None:
            return self.send_json(400, {"error": "issue 标识不合法"})
        repo, token = load_tracker()
        if not repo or not token:
            return self.send_json(503, {"error": "GitHub 未配置"})
        issue = fetch_issue(repo, token, number)
        if issue is None:
            return self.send_json(502, {"error": "读取 issue 失败"})
        if not issue_trusted(issue):
            return self.send_json(403, {"error": "该 issue 作者不在受信任范围"})
        if action == "add" and (issue.get("state") or "") != "open":
            return self.send_json(400, {"error": "issue 不是 open 状态，调度器不会派发"})
        labels = {str(l.get("name") or "").lower() for l in issue.get("labels") or []}
        posted = False
        if comment:
            try:
                github_request("POST", "/repos/%s/issues/%s/comments" % (repo, number), token, {"body": comment})
                posted = True
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                return self.send_json(502, {"error": "评论发布失败 GitHub %s: %s" % (exc.code, detail)})
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
                return self.send_json(502, {"error": "评论发布失败: %s" % exc})
        others = [name for name in ("agent", "needs-human") if name != label]
        if action == "add":
            # 执行/暂停标签互斥：加其中一个前先摘掉已在的其他标签。
            for other in [name for name in others if name in labels]:
                try:
                    github_request("DELETE", "/repos/%s/issues/%s/labels/%s" % (repo, number, other), token)
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        detail = exc.read().decode("utf-8", "replace")[:200]
                        prefix = "评论已发布，但" if posted else ""
                        return self.send_json(
                            502, {"error": "%s摘掉 %s 标签失败 GitHub %s: %s" % (prefix, other, exc.code, detail)}
                        )
                except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                    prefix = "评论已发布，但" if posted else ""
                    return self.send_json(502, {"error": "%s摘掉 %s 标签失败: %s" % (prefix, other, exc)})
        try:
            if action == "add":
                github_request("POST", "/repos/%s/issues/%s/labels" % (repo, number), token, {"labels": [label]})
            else:
                github_request("DELETE", "/repos/%s/issues/%s/labels/%s" % (repo, number, label), token)
        except urllib.error.HTTPError as exc:
            if action == "remove" and exc.code == 404:
                pass
            else:
                detail = exc.read().decode("utf-8", "replace")[:200]
                prefix = "评论已发布，但标签操作失败：" if posted else ""
                return self.send_json(502, {"error": prefix + "GitHub %s: %s" % (exc.code, detail)})
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            prefix = "评论已发布，但标签操作失败：" if posted else ""
            return self.send_json(502, {"error": prefix + "GitHub 请求失败: %s" % exc})
        clear_github_cache()
        if action == "add" and label == "agent":
            message = (
                "已发布说明并加入 agent 标签，调度器会在下个轮询周期派发"
                if posted
                else "已加入 agent 标签，调度器会在下个轮询周期派发"
            )
        elif action == "remove" and label == "agent":
            message = "已移除 agent 标签，调度器会在下个轮询周期停止该任务"
        elif action == "add":
            message = "已加入 needs-human 并移除执行标签；该 issue 暂停等待人工"
        else:
            message = "已移除 needs-human 标签"
        return self.send_json(200, {"ok": True, "message": message, "commented": posted})

    def handle_intervene(self):
        """人工介入：pause 加 needs-human 并摘 agent；resume 反向恢复。"""
        if not self.write_guard():
            return
        payload = self.read_json_body()
        if payload is None:
            return
        op = str(payload.get("op") or "")
        if op not in ("pause", "resume"):
            return self.send_json(400, {"error": "op 必须是 pause 或 resume"})
        comment = str(payload.get("comment") or "").strip()
        if len(comment) > 4000:
            return self.send_json(400, {"error": "说明过长（上限 4000 字）"})
        number = self.issue_number(payload.get("issue"))
        if number is None:
            return self.send_json(400, {"error": "issue 标识不合法"})
        repo, token = load_tracker()
        if not repo or not token:
            return self.send_json(503, {"error": "GitHub 未配置"})
        issue = fetch_issue(repo, token, number)
        if issue is None:
            return self.send_json(502, {"error": "读取 issue 失败"})
        if not issue_trusted(issue):
            return self.send_json(403, {"error": "该 issue 作者不在受信任范围"})
        if (issue.get("state") or "") != "open":
            return self.send_json(400, {"error": "issue 不是 open 状态"})
        labels = {str(l.get("name") or "").lower() for l in issue.get("labels") or []}
        if op == "pause" and "needs-human" in labels and "agent" not in labels:
            return self.send_json(400, {"error": "该 issue 已在等待人工处理"})
        if op == "resume" and "needs-human" not in labels:
            return self.send_json(400, {"error": "该 issue 没有待处理的 needs-human 标签"})

        posted = False
        if comment:
            try:
                github_request("POST", "/repos/%s/issues/%s/comments" % (repo, number), token, {"body": comment})
                posted = True
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                return self.send_json(502, {"error": "评论发布失败 GitHub %s: %s" % (exc.code, detail)})
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
                return self.send_json(502, {"error": "评论发布失败: %s" % exc})

        def swap(add_label, drop_label):
            # 顺序与宿主一致：先加暂停标记再摘执行标签（pause），恢复时反向，
            # 避免出现 agent + needs-human 的无效组合被调度器看到。
            try:
                if add_label:
                    github_request("POST", "/repos/%s/issues/%s/labels" % (repo, number), token, {"labels": [add_label]})
                if drop_label:
                    github_request("DELETE", "/repos/%s/issues/%s/labels/%s" % (repo, number, drop_label), token)
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and drop_label:
                    pass
                else:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                    prefix = "评论已发布，但" if posted else ""
                    return "%s标签操作失败 GitHub %s: %s" % (prefix, exc.code, detail)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                prefix = "评论已发布，但" if posted else ""
                return "%s标签操作失败: %s" % (prefix, exc)
            return None

        if op == "pause":
            error = swap("needs-human", "agent" if "agent" in labels else None)
        else:
            error = swap(None, "needs-human")
            if error is None:
                error = swap("agent", None)
        if error:
            return self.send_json(502, {"error": error})
        clear_github_cache()
        if op == "pause":
            message = (
                "已暂停并标记 needs-human（%s）调度器不会再派发；处理完成后点“恢复执行”"
                % ("已留言，" if posted else "")
            )
        else:
            message = "已移除 needs-human 并重新加 agent；调度器会重新领取，合并前宿主会重新核验当前 SHA 与审批"
        return self.send_json(200, {"ok": True, "message": message, "commented": posted})

    def handle_tail(self, issue, query):
        if not ISSUE_RE.match(issue or ""):
            return self.send_json(400, {"error": "bad issue name"})

        entries = scan_sessions().get(issue) or []
        if not entries:
            return self.send_json(404, {"error": "no recorded runs"})

        run = entries[-1]
        raw_offset = query.get("offset", [""])[0]
        offset = int(raw_offset) if raw_offset.isdigit() else None
        src = query.get("src", [""])[0][:32]
        if run.get("live"):
            chunk = tail_live(run["path"], offset, src)
        else:
            chunk = tail_chunk(run["path"], offset)

        entry = agent_detail(symphony_state()).get(issue)
        running = bool(entry) or (time.time() - run["mtime"]) < LIVE_WINDOW
        agent = None
        if entry:
            tokens = entry.get("tokens") or {}
            agent = {
                "message": entry.get("last_message") or "",
                "event": entry.get("last_event") or "",
                "event_at": entry.get("last_event_at") or "",
                "started_at": entry.get("started_at") or "",
                "turns": entry.get("turn_count"),
                "tokens": tokens.get("total_tokens"),
            }

        return self.send_json(
            200,
            {
                "offset": chunk["offset"],
                "start": chunk["start"],
                "html": chunk["html"],
                "reset": bool(chunk.get("reset")),
                "running": running,
                "pending": chunk["pending"],
                "session": run["short"],
                "agent": agent,
            },
        )

    def handle_issues(self):
        data = build_cards()
        if data is None:
            return self.send_json(503, {"error": "github unavailable"})
        counts = data["counts"]
        summary = " · ".join(
            "%d %s" % (counts.get(key, 0), label)
            for key, label, _sub in COLUMNS
            if counts.get(key, 0)
        ) or "当前没有任务"
        return self.send_json(
            200,
            {
                "cols": data["cols"],
                "summary": summary,
                "counts": {
                    "total": data["total"],
                    "running": counts.get("running", 0),
                    "blocked": counts.get("blocked", 0),
                    "retrying": counts.get("retrying", 0),
                    "ready": counts.get("ready", 0),
                    "awaiting": counts.get("awaiting", 0),
                    "with_runs": data["with_runs"],
                },
                "repo": data["repo"],
                "writable": bool(data.get("writable")),
                "updated": datetime.now(CN).strftime("%H:%M:%S"),
            },
        )

    def handle_runs(self, issue):
        if not ISSUE_RE.match(issue or ""):
            return self.send_json(400, {"error": "bad issue name"})

        entries = scan_sessions().get(issue) or []
        state = symphony_state()
        agent = agent_detail(state).get(issue)
        trouble = agent_errors(state).get(issue)
        now = time.time()
        latest = entries[-1] if entries else None
        runs = []
        for r in reversed(entries):
            active = (r is latest and agent is not None) or (now - r["mtime"]) < LIVE_WINDOW
            runs.append(
                {
                    "short": r["short"],
                    "started": r["started"],
                    "ended": datetime.fromtimestamp(r["mtime"], CN).strftime("%Y-%m-%d %H:%M:%S"),
                    "size": human_size(r["size"]),
                    "messages": r["messages"],
                    "live": bool(r.get("live")),
                    "active": active,
                }
            )
        agent_info = None
        if agent:
            tokens = agent.get("tokens") or {}
            agent_info = {
                "message": agent.get("last_message") or "",
                "event": agent.get("last_event") or "",
                "event_at": agent.get("last_event_at") or "",
                "started_at": agent.get("started_at") or "",
                "tokens": tokens.get("total_tokens"),
            }
        return self.send_json(
            200,
            {"issue": issue, "agent": agent_info, "trouble": trouble, "runs": runs},
        )


    def handle_run_detail(self, issue, short, query):
        if not ISSUE_RE.match(issue or "") or not ISSUE_RE.match(short or ""):
            return self.send_json(400, {"error": "bad run reference"})

        entries = scan_sessions().get(issue) or []
        run = next((r for r in entries if r["short"].startswith(short)), None)
        if not run:
            return self.send_json(404, {"error": "no such run"})

        raw_offset = query.get("offset", [""])[0]
        offset = int(raw_offset) if raw_offset.isdigit() else None
        src = query.get("src", [""])[0][:32]
        if run.get("live"):
            chunk = tail_live(run["path"], offset, src)
        else:
            chunk = tail_chunk(run["path"], offset)

        latest_and_running = entries[-1] is run and agent_detail(symphony_state()).get(issue)
        active = bool(latest_and_running) or (time.time() - run["mtime"]) < LIVE_WINDOW

        eof = chunk["offset"] >= chunk["size"] if chunk["size"] else True

        return self.send_json(
            200,
            {
                "offset": chunk["offset"],
                "start": chunk["start"],
                "size": chunk["size"],
                "html": chunk["html"],
                "ops": chunk.get("ops") or [],
                "reset": bool(chunk.get("reset")),
                "active": active,
                "eof": eof,
                "truncated": chunk["truncated"],
                "pending": chunk["pending"],
            },
        )

    def serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        root = os.path.realpath(HTML_DIR)
        target = os.path.realpath(os.path.join(root, rel))
        if not target.startswith(root + os.sep) and target != root:
            return self.send_text(403, "forbidden")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return self.send_text(404, "not found")

        ext = os.path.splitext(target)[1].lower()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
        }.get(ext, "application/octet-stream")

        with open(target, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("content-type", mime)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status, body):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_text(self, status, text):
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, data):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    parser.add_argument("--bind", default="192.168.1.199")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    print("board on http://%s:%d/" % (args.bind, args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

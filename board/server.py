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
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SESS_DIR = os.environ.get("SYMPHONY_SESSIONS", "/root/symphony/sessions")
LIVE_DIR = os.environ.get("SYMPHONY_SESSIONS_LIVE", "/root/symphony/sessions-live")
HTML_DIR = os.path.join(SESS_DIR, "html")
WORKFLOW = os.environ.get("SYMPHONY_WORKFLOW", "/root/symphony/config/WORKFLOW.md")
STATE_URL = os.environ.get("SYMPHONY_STATE_URL", "http://127.0.0.1:8320/api/v1/state")
STATE_POLL_INTERVAL = 2.0
IDENTIFIER_PREFIX = "GH-"

MAX_CHUNK = 256 * 1024
HEAD_BYTES = 1024 * 1024
LIVE_WINDOW = 15.0
LIVE_RETENTION_S = 30 * 86400
TEXT_LIMIT = 4000
THINK_LIMIT = 1500
ISSUE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REPO_RE = re.compile(r'^\s*repo:\s*"([^"]+)"', re.M)
MODEL_RE = re.compile(r'^\s*model:\s*"?([^"\s]+)"?\s*$', re.M)
THINKING_RE = re.compile(r'^\s*thinking:\s*"?([^"\s]+)"?\s*$', re.M)
CN = timezone(timedelta(hours=8))

ISSUE_CACHE_TTL = 60.0
DONE_COL_LIMIT = 8
SESSION_CACHE_TTL = 20.0
GITHUB_POLL_INTERVAL = float(os.environ.get("SYMPHONY_GITHUB_POLL_S", "30"))
_cache = {"issues": None, "issues_at": 0.0, "sessions": None, "sessions_at": 0.0}
_task_lock = threading.Lock()

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
.runtime{flex:none;display:flex;align-items:center;gap:6px;font-size:10.5px;color:var(--fg3);border:1px solid var(--edge);border-radius:10px;padding:2px 8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:280px}
.runtime b{color:var(--fg2);font-weight:500}
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
.col{display:flex;flex-direction:column;flex:1 1 0;min-width:232px;max-width:360px;min-height:0;border:1px solid var(--edge);border-radius:8px;background:var(--panel)}
.col[hidden]{display:none}
.col.is-empty .empty-col{padding:14px 10px}
.bcard.dragging{opacity:.45}
.col.drop-target{border-color:var(--info);background:rgba(56,189,248,.06)}
.col.drop-target .chead{color:var(--info)}
.col-head{flex:none;display:flex;align-items:flex-start;gap:8px;padding:10px 12px 8px}
.cdot{flex:none;width:6px;height:6px;border-radius:50%;margin-top:6px;background:var(--fg3)}
.cdot.backlog{background:var(--fg3)}
.cdot.todo{background:var(--info)}
.cdot.in_progress{background:var(--warn);animation:pulse 1.5s infinite}
.cdot.review{background:var(--info)}
.cdot.done{background:var(--ok)}
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
.tone-done{color:var(--ok)}
#shade{position:fixed;inset:0;z-index:40;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .15s}
#shade.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;bottom:0;z-index:41;display:flex;flex-direction:column;width:720px;max-width:94vw;background:var(--panel);border-left:1px solid var(--edge);box-shadow:-24px 0 70px rgba(0,0,0,.55);opacity:0;transform:translateX(28px);pointer-events:none;transition:opacity .18s,transform .18s ease-out}
.drawer.open{opacity:1;transform:none;pointer-events:auto}
.dgrip{position:absolute;top:0;left:-5px;bottom:0;width:11px;cursor:col-resize;touch-action:none;z-index:2}
.dgrip::after{content:'';position:absolute;top:0;left:5px;bottom:0;width:2px;background:transparent;transition:background .12s}
.dgrip:hover::after,body.resizing .dgrip::after{background:rgba(56,189,248,.5)}
body.resizing{user-select:none;cursor:col-resize}
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
.action-toolbar{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.action-toolbar>.abtns{flex:1;min-width:0}
.action-more{position:relative;flex:none}
.action-more>summary{display:flex;align-items:center;gap:8px;list-style:none;min-height:28px;box-sizing:border-box;padding:4px 8px;color:var(--fg2);font:11px var(--sans);cursor:pointer;border:1px solid transparent;border-radius:5px}
.action-more>summary::-webkit-details-marker{display:none}
.action-more>summary::after{content:'';width:5px;height:5px;border-right:1px solid currentColor;border-bottom:1px solid currentColor;transform:rotate(45deg);margin-top:-3px}
.action-more[open]>summary::after{transform:rotate(225deg);margin-top:3px}
.action-more>summary:hover,.action-more[open]>summary{background:var(--elev);color:var(--fg)}
.action-more>summary:focus-visible{outline:2px solid var(--info);outline-offset:2px}
.action-menu{position:absolute;right:0;top:calc(100% + 6px);z-index:20;width:min(260px,calc(100vw - 56px));box-sizing:border-box;padding:6px;border:1px solid var(--edge2);border-radius:6px;background:var(--elev);box-shadow:0 8px 24px #0005}
.action-menu .abtn{display:block;width:100%;text-align:left;border-color:transparent;min-height:34px}
.actions .action-menu .an{margin:2px 10px 6px;line-height:1.6}
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
#toast{position:fixed;left:50%;bottom:24px;z-index:60;display:flex;align-items:center;gap:9px;max-width:min(560px,calc(100vw - 32px));padding:10px 14px;border-radius:9px;border:1px solid var(--edge2);background:linear-gradient(180deg,#242424,#1b1b1b);color:var(--fg);font-size:12px;box-shadow:0 16px 44px rgba(0,0,0,.6);opacity:0;transform:translate(-50%,10px);pointer-events:none;transition:opacity .18s,transform .18s}
#toast.on{opacity:1;transform:translate(-50%,0)}
#toast .ticon{flex:none;display:inline-flex;width:16px;height:16px;color:var(--ok)}
#toast .ticon svg{width:16px;height:16px;display:block}
#toast .tmsg{min-width:0;overflow-wrap:anywhere}
#toast.err{border-color:rgba(251,113,133,.45);background:linear-gradient(180deg,#2a1c1e,#201618)}
#toast.err .ticon{color:var(--danger)}
body.dialog-open{overflow:hidden}
#modal{position:fixed;inset:0;z-index:70;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(8,8,8,.66);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);opacity:0;visibility:hidden;transition:opacity .16s,visibility .16s}
#modal.open{opacity:1;visibility:visible}
.dialog{position:relative;width:min(460px,100%);max-height:calc(100dvh - 40px);overflow-y:auto;background:linear-gradient(180deg,#212121,#1a1a1a);border:1px solid var(--edge2);border-radius:14px;box-shadow:0 32px 90px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.04);transform:translateY(12px) scale(.985);opacity:.5;transition:transform .2s cubic-bezier(.2,.8,.2,1),opacity .2s}
#modal.open .dialog{transform:none;opacity:1}
.dlg-head{display:flex;gap:12px;padding:20px 20px 0}
.dlg-icon{flex:none;display:flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:9px;border:1px solid rgba(56,189,248,.25);background:rgba(56,189,248,.12);color:var(--info)}
.dlg-icon svg{width:18px;height:18px;display:block}
.dlg-icon.warn{border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.12);color:var(--warn)}
.dlg-icon.danger{border-color:rgba(251,113,133,.3);background:rgba(251,113,133,.12);color:var(--danger)}
.dlg-icon.ok{border-color:rgba(52,211,153,.3);background:rgba(52,211,153,.12);color:var(--ok)}
.dlg-copy{min-width:0;flex:1}
.dlg-title{margin:1px 0 0;font-size:14.5px;font-weight:600;line-height:1.4;color:var(--fg);word-break:break-word}
.dlg-body{margin:5px 0 0;font-size:12px;line-height:1.65;color:var(--fg2);white-space:pre-line;overflow-wrap:anywhere}
.dlg-field{position:relative;margin:14px 20px 0}
.dlg-field textarea{display:block;width:100%;min-height:84px;max-height:220px;padding:9px 10px 22px;border:1px solid var(--edge2);border-radius:8px;background:var(--sunken);color:var(--fg);font:12px/1.6 var(--sans);outline:none;resize:vertical;transition:border-color .12s,box-shadow .12s}
.dlg-field textarea:focus{border-color:rgba(56,189,248,.55);box-shadow:0 0 0 3px rgba(56,189,248,.12)}
.dlg-field textarea::placeholder{color:var(--fg3)}
.dlg-count{position:absolute;right:10px;bottom:7px;font:10px var(--mono);color:var(--fg3);pointer-events:none}
.dlg-count.limit{color:var(--danger)}
.dlg-actions{display:flex;justify-content:flex-end;gap:8px;padding:18px 20px 20px}
.dlg-btn{min-width:84px;min-height:34px;padding:6px 14px;border:1px solid var(--edge2);border-radius:7px;background:transparent;color:var(--fg2);font:12px var(--sans);cursor:pointer;transition:background .12s,color .12s,border-color .12s,filter .12s}
.dlg-btn:hover{background:var(--elev);color:var(--fg);border-color:var(--fg3)}
.dlg-btn:focus-visible{outline:2px solid var(--info);outline-offset:2px}
.dlg-btn.primary{background:var(--fg);border-color:var(--fg);color:#111;font-weight:600}
.dlg-btn.primary:hover{filter:brightness(1.08);background:var(--fg);border-color:var(--fg);color:#111}
.dlg-btn.primary.ok{background:var(--ok);border-color:var(--ok);color:#04150f}
.dlg-btn.primary.warn{background:var(--warn);border-color:var(--warn);color:#1f1700}
.dlg-btn.primary.danger{background:var(--danger);border-color:var(--danger);color:#21060a}
.mgrid{margin-top:10px;display:grid;grid-template-columns:1fr 1fr;gap:10px 20px;border:1px solid var(--edge);border-radius:6px;background:var(--card);padding:11px 12px;font-size:11px}
.mi{display:flex;flex-direction:column;gap:1px;min-width:0}
.mi span{color:var(--fg3)}
.mi b{font-family:var(--mono);font-weight:400;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mpr{color:var(--info)}
.mpr:hover{text-decoration:underline}
.dnote{margin:14px 0 2px;font-size:11px;color:var(--fg3)}
.dnote.warn{color:var(--warn)}
.dsub{margin-top:3px;font-size:11px;color:var(--fg3);overflow-wrap:anywhere}
.dgrid{margin-top:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px 20px;border:1px solid var(--edge);border-radius:6px;background:var(--card);padding:11px 12px}
.dk{font-size:10px;color:var(--fg3);letter-spacing:.02em}
.dv{margin-top:1px;font-size:12px;color:var(--fg);overflow-wrap:anywhere}
.dpre{margin:6px 0 0;padding:10px 12px;border:1px solid var(--edge);border-radius:6px;background:var(--card);color:var(--fg2);font:11px/1.6 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;max-height:340px;overflow:auto}
.dactions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.drun{padding:6px 0;border-bottom:1px solid var(--edge);font-size:11px}
.drun:last-child{border-bottom:0}
.drun a{color:var(--fg2);text-decoration:none}
.drun a:hover{color:var(--fg)}
.drun.live a{color:var(--ok)}
.crow{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px;padding:8px 12px;border-bottom:1px solid var(--edge);font-size:12px;color:var(--fg2);overflow-wrap:anywhere}
.crow:last-child{border-bottom:0}
.crow b{color:var(--fg);font-weight:600}
.cnote{font-size:11px;color:var(--fg3)}
.runbar{display:flex;flex-wrap:wrap;gap:6px 18px;margin:2px 0 14px;font-size:11.5px;color:var(--fg3)}
.runbar b{color:var(--fg);font-weight:600}
.runlist{border:1px solid var(--edge);border-radius:8px;background:var(--panel);overflow:hidden}
.runrow{display:flex;align-items:center;gap:10px;padding:11px 14px;border-bottom:1px solid var(--edge);text-decoration:none;color:inherit;transition:background .12s}
.runrow:last-child{border-bottom:0}
.runrow:hover{background:var(--elev)}
.runrow .dot{flex:none;width:7px;height:7px;border-radius:50%;background:var(--fg3)}
.runrow .dot.live{background:var(--ok)}
.rr-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.rr-time{display:flex;align-items:center;gap:8px;font:12px var(--mono);color:var(--fg)}
.rr-sub{font-size:11px;color:var(--fg3)}
.rr-go{flex:none;color:var(--fg3);font-size:16px;line-height:1}
.runrow:hover .rr-go{color:var(--info)}
.state-idle{opacity:.92}
.bquick{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;opacity:0;transition:opacity .12s}.bcard:hover .bquick,.bcard:focus-within .bquick{opacity:1}
.bqbtn{min-height:24px;padding:2px 8px;border:1px solid var(--edge2);border-radius:5px;background:transparent;color:var(--fg2);font:11px var(--sans);cursor:pointer;transition:color .12s,border-color .12s}
.bqbtn:hover{color:var(--fg);border-color:var(--fg3)}
.bqbtn.back:hover{color:var(--warn);border-color:rgba(251,191,36,.35)}
.dhist{margin-top:6px;border:1px solid var(--edge);border-radius:6px;background:var(--card);padding:4px 12px}.dhrow{display:flex;align-items:baseline;gap:8px;padding:7px 0;border-bottom:1px solid var(--edge);font-size:11.5px;color:var(--fg2)}
.dhrow:last-child{border-bottom:0}
.dhdot{flex:none;width:5px;height:5px;border-radius:50%;background:var(--edge2);margin-top:5px}
.dhmain{flex:1;min-width:0;overflow-wrap:anywhere}
.dhmain b{color:var(--fg);font-weight:600}
.dhby{color:var(--fg3)}
.dhtime{flex:none;font:10.5px var(--mono);color:var(--fg3);white-space:nowrap}
#dcomment{display:block;box-sizing:border-box;width:100%;min-height:72px;max-height:220px;margin-top:6px;padding:9px 10px;border:1px solid var(--edge2);border-radius:8px;background:var(--sunken);color:var(--fg);font:12px/1.6 var(--sans);outline:none;resize:vertical;transition:border-color .12s,box-shadow .12s}
#dcomment:focus{border-color:rgba(56,189,248,.55);box-shadow:0 0 0 3px rgba(56,189,248,.12)}
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
.msg{margin:0;padding:10px 14px;border-bottom:1px solid var(--edge);transition:background .12s}
#feed .msg:hover{background:rgba(255,255,255,.025)}
.msg[data-role="user"]{background:rgba(56,189,248,.05);box-shadow:inset 2px 0 0 rgba(56,189,248,.55)}
.msg[data-role="tool"]{box-shadow:inset 2px 0 0 rgba(167,139,250,.4)}
.msg[data-role="meta"]{background:rgba(251,113,133,.06)}
.msg.collapsed .ln.say,.msg.collapsed .ln.user,.msg.collapsed .ln.tool .out{max-height:240px;overflow:hidden;-webkit-mask-image:linear-gradient(#000 65%,transparent);mask-image:linear-gradient(#000 65%,transparent)}
.msg .expand{margin-top:4px;font-size:11px;color:var(--info);cursor:pointer;background:none;border:0;padding:0}
.msg .expand:hover{text-decoration:underline}
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
 .topbar{padding:0 10px;gap:8px}.auto{gap:4px}.auto>a{max-width:100px;overflow:hidden;text-overflow:ellipsis}.auto>svg,.auto>span{display:none}.runtime{display:none}
 .head{padding:10px}.head h1{font-size:16px}.head .note{font-size:11px;line-height:1.4}
 .tools{padding:8px 10px;gap:8px}.tools>.chips{display:none}
 .view-tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));width:100%;order:-1;gap:6px}
 .view-tabs .chip{justify-content:center;padding:8px 4px;font-size:13px;min-height:44px}
 .search{flex:1;min-width:0;width:auto;max-width:none}.search input{font-size:16px;min-height:44px}
 .chk{min-height:44px}.chip,.abtn,.logbtn,.fbtn{min-height:44px}
 .board{padding:10px;gap:10px;scroll-snap-type:x proximity}.col{min-width:min(84vw,340px);scroll-snap-align:start}
 .completed-panel{padding:12px 10px;padding-bottom:max(16px,env(safe-area-inset-bottom))}
 .completed-list{grid-template-columns:minmax(0,1fr);gap:10px}.completed-card{padding:14px}.completed-card .btitle{font-size:15px}
 .drawer{width:100vw !important;max-width:100vw;height:100dvh;border-left:0}
 .dgrip{display:none}
 .dhead{padding:10px;padding-top:max(10px,env(safe-area-inset-top))}.dclose,.dext{min-width:44px;min-height:44px}
 .dbody{padding:12px 10px;padding-bottom:max(24px,env(safe-area-inset-bottom))}.dtitle{font-size:16px}
 .acmt{font-size:16px}.abtns .abtn{flex:1;min-width:100px;font-size:13px}.mgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .run-head{flex-wrap:wrap}.run-time{white-space:normal;overflow-wrap:anywhere}.log-body{max-height:55dvh}
 #modal{padding:12px;align-items:flex-end}
 .dialog{width:100%;max-height:calc(100dvh - 24px)}
 .dlg-head{padding:18px 16px 0}
 .dlg-field{margin:12px 16px 0}.dlg-field textarea{font-size:16px}
 .dlg-actions{padding:16px;padding-bottom:max(16px,env(safe-area-inset-bottom))}
 .dlg-actions .dlg-btn{flex:1;min-height:44px;font-size:13px}
}
"""

BOARD = """
<script>
const COLS = ['backlog', 'todo', 'in_progress', 'human_review', 'merging', 'rework', 'done'];
const EMPTY = {
  backlog: '没有未排期的任务',
  todo: '没有等待领取的任务',
  in_progress: '没有正在进行的任务',
  human_review: '没有等待人工审核的交付',
  merging: '没有等待合并的任务',
  rework: '没有退回重做的任务',
  done: '没有已结束的任务'
};
const STATES = ['Backlog', 'Todo', 'In Progress', 'Human Review', 'Merging',
  'Rework', 'Done', 'Cancelled', 'Closed', 'Duplicate'];
const boardEl = document.getElementById('board');
let CUR = null, RUNS = [], AGENT = null, TROUBLE = null, FACTS = {};
let BUSY = false, TOAST_TIMER = null, OPEN_SEQ = 0;
let CUR_REV = null, CUR_SOURCE = null, CUR_SOURCE_ERR = null;
const PENDING = new Map();
const POSTED = new Map();
let SHOW_EMPTY = true;

function esc(s) {
  const d = document.createElement('div');
  d.textContent = (s === null || s === undefined) ? '' : String(s);
  return d.innerHTML;
}

function showToast(kind, text) {
  const t = document.getElementById('toast');
  t.className = 'show ' + kind;
  t.querySelector('.tmsg').textContent = text;
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(function () { t.className = ''; }, 4200);
}

function resolvePending(facts) {
  for (const [issue, pending] of PENDING) {
    const f = (facts || {})[issue];
    if (f && f.state === pending.state) { PENDING.delete(issue); continue; }
    if (Date.now() - pending.at > 60000) {
      PENDING.delete(issue);
      showToast('warn', issue + ' 状态未按预期更新，请刷新核对');
    }
  }
}

function applyData(d) {
  COLS.forEach(function (k) {
    const body = document.getElementById('col-' + k);
    const html = (d.cols || {})[k] || '';
    body.innerHTML = html || ("<div class='empty-col'>" + EMPTY[k] + '</div>');
    const col = document.querySelector(".col[data-col='" + k + "']");
    document.getElementById('cc-' + k).textContent = ((d.counts || {})[k]) || 0;
    col.classList.toggle('is-empty', !html);
  });
  document.getElementById('sum').textContent = d.summary || '';
  const all = document.getElementById('chipAll');
  if (all) { all.textContent = (d.counts || {}).total || 0; }
  FACTS = d.facts || {};
  resolvePending(FACTS);
  document.getElementById('health').textContent =
    d.sync_error ? d.sync_error : ('更新于 ' + (d.updated || '—'));
  applyFilter();
  applyShowEmpty();
}

async function fetchIssues() {
  const res = await fetch('/api/issues', {cache: 'no-store'});
  if (res.status === 409) { return fetchIssues(); }
  if (!res.ok) { throw new Error('status ' + res.status); }
  applyData(await res.json());
}
function refreshIssues() {
  return fetchIssues().catch(function () {
    document.getElementById('health').textContent = '调度器不可用 · 保留上次状态';
  });
}

async function postTask(body) {
  const res = await fetch('/api/task', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  return {status: res.status, data: data};
}

function displayedRev(issue) {
  // The revision the user actually sees: a stale background refresh must
  // never upgrade the submission basis of an open drawer behind the user's
  // back. Falls back to FACTS only when the drawer is not the caller.
  if (issue === CUR && CUR_REV !== null && CUR_REV !== undefined) {
    return CUR_REV;
  }
  return (FACTS[issue] || {}).rev;
}

function redrawDrawer(issue) {
  // Re-render with the facts the user is about to see, and rebind the
  // submission revision to that visible revision.
  const f = FACTS[issue] || {};
  CUR_REV = (f.rev === undefined || f.rev === null) ? null : f.rev;
  renderDrawer(f, CUR_SOURCE, CUR_SOURCE_ERR);
}

async function submitMove(issue, target, comment) {
  if (BUSY) { return; }
  BUSY = true;
  try {
    let commentToSend = comment || '';
    if (commentToSend && POSTED.get(issue) === commentToSend) {
      commentToSend = '';  // already published by a prior attempt — do not repost
    }
    const r = await postTask({issue: issue, op: 'move', state: target,
      expected_revision: displayedRev(issue), comment: commentToSend});
    if (r.status === 200) {
      POSTED.delete(issue);
      PENDING.set(issue, {state: target, at: Date.now()});
      const f = FACTS[issue] || {};
      FACTS[issue] = Object.assign({}, f, {
        state: r.data.state || target,
        rev: (r.data.revision === undefined || r.data.revision === null)
          ? f.rev : r.data.revision});
      if (issue === CUR) { redrawDrawer(issue); }
      showToast('ok', r.data.message || ('已移动到 ' + target));
      refreshIssues();
      return;
    }
    if (r.status === 409) {
      if (r.data.comment_posted && commentToSend) {
        POSTED.set(issue, commentToSend);
      }
      showToast('warn', r.data.comment_posted
        ? '评论已发布（不会重复发送）；任务状态已变化，已载入最新修订，请重新决定'
        : '任务状态已变化，已载入最新修订，请重新决定');
      await refreshIssues();
      if (issue === CUR && CUR) { redrawDrawer(issue); }
      return;
    }
    showToast('err', r.data.error || ('操作失败（' + r.status + '）'));
    if (r.data.outcome && r.data.outcome !== 'not_applied') { refreshIssues(); }
  } catch (e) {
    showToast('err', '请求失败：' + (e && e.message ? e.message : '网络异常'));
  } finally { BUSY = false; }
}

async function submitComment(issue, comment) {
  if (BUSY) { return; }
  if (POSTED.get(issue) === comment) {
    showToast('warn', '该评论已发布，未重复发送');
    return;
  }
  BUSY = true;
  try {
    const r = await postTask({issue: issue, op: 'comment', comment: comment});
    if (r.status === 200) {
      POSTED.set(issue, comment);
      showToast('ok', r.data.message || '评论已记录');
      refreshIssues();
      return;
    }
    if (r.status === 409 && r.data.comment_posted) {
      POSTED.set(issue, comment);
      showToast('warn', '评论已发布，状态未变化');
      refreshIssues();
      return;
    }
    showToast('err', r.data.error || ('评论失败（' + r.status + '）'));
    if (r.data.outcome && r.data.outcome !== 'not_applied') { refreshIssues(); }
  } catch (e) {
    showToast('err', '请求失败：' + (e && e.message ? e.message : '网络异常'));
  } finally { BUSY = false; }
}

async function openIssue(issue) {
  CUR = issue;
  const seq = ++OPEN_SEQ;
  const drawer = document.getElementById('drawer');
  const dhead = document.getElementById('dhead');
  const dbody = document.getElementById('dbody');
  document.getElementById('shade').className = 'on';
  drawer.classList.add('open');
  dhead.innerHTML = "<button class='dclose' id='dclose' title='关闭'>&#8249;</button>" +
    "<div class='dtitle'>" + esc(issue) + '</div>';
  dbody.innerHTML = "<div class='dnote'>加载中…</div>";
  let runs = {runs: [], agent: null, trouble: null};
  let source = null, sourceErr = null;
  // 先渲染本地已有数据（状态/修订/标签/分支），GitHub 内容后补，
  // 避免抽屉打开前等一次完整网络往返。
  const fact = FACTS[issue] || {};
  CUR_REV = (fact.rev === undefined || fact.rev === null) ? null : fact.rev;
  const preCard = boardEl.querySelector(".bcard[data-issue='" + issue + "']");
  const preTitle = preCard ? preCard.getAttribute('data-title') : issue;
  dhead.innerHTML = "<button class='dclose' id='dclose' title='关闭'>&#8249;</button>" +
    "<div class='dtitle'>" + esc(preTitle || issue) +
    "<div class='dsub'>" + esc(issue) + '</div></div>';
  renderDrawer(fact, null, null, true);
  const [runsRes, srcRes] = await Promise.allSettled([
    fetch('/api/runs/' + encodeURIComponent(issue)).then(function (r) { return r.json(); }),
    fetch('/api/source/' + encodeURIComponent(issue)).then(async function (r) {
      const body = await r.json().catch(function () { return {}; });
      return {ok: r.ok, status: r.status, body: body};
    })
  ]);
  if (runsRes.status === 'fulfilled' && runsRes.value) { runs = runsRes.value; }
  if (srcRes.status === 'fulfilled' && srcRes.value) {
    if (srcRes.value.ok) { source = srcRes.value.body; }
    else { sourceErr = srcRes.value.body.error || ('来源接口返回 ' + srcRes.value.status); }
  } else {
    sourceErr = '网络异常';
  }
  if (seq !== OPEN_SEQ || CUR !== issue) { return; }
  RUNS = runs.runs || [];
  AGENT = runs.agent;
  TROUBLE = runs.trouble;
  CUR_SOURCE = source;
  CUR_SOURCE_ERR = sourceErr;
  const f = FACTS[issue] || {};
  CUR_REV = (f.rev === undefined || f.rev === null) ? null : f.rev;
  const card = boardEl.querySelector(".bcard[data-issue='" + issue + "']");
  const title = card ? card.getAttribute('data-title') : issue;
  const url = card ? card.getAttribute('data-url') : '';
  const pr = card ? card.getAttribute('data-pr') : '';
  const prnum = card ? card.getAttribute('data-pr-num') : '';
  const links = [];
  if (url) { links.push("<a href='" + esc(url) + "' target='_blank' rel='noopener noreferrer'>Issue</a>"); }
  if (pr) { links.push("<a href='" + esc(pr) + "' target='_blank' rel='noopener noreferrer'>PR #" + esc(prnum) + '</a>'); }
  links.push("<a href='/issue/" + encodeURIComponent(issue) + "'>运行记录</a>");
  links.push("<a href='/live/" + encodeURIComponent(issue) + "'>实时视图</a>");
  dhead.innerHTML = "<button class='dclose' id='dclose' title='关闭'>&#8249;</button>" +
    "<div class='dtitle'>" + esc(title || issue) +
    "<div class='dsub'>" + esc(issue) + ' · ' + links.join(' · ') + '</div></div>';
  renderDrawer(f, source, sourceErr);
}

function renderDrawer(f, source, sourceError, loading) {
  const dbody = document.getElementById('dbody');
  const state = f.state || 'Backlog';
  const rev = (f.rev === undefined || f.rev === null) ? '—' : f.rev;
  const meta = [
    ['状态', state], ['修订', rev],
    ['源状态', f.closed ? 'closed（源已关闭，本地状态仍以任务记录为准）' : 'open']
  ];
  if (f.branch) { meta.push(['分支', f.branch]); }
  if (f.workpad) { meta.push(['Workpad', 'comment #' + f.workpad]); }
  if (f.blocked) { meta.push(['阻塞原因', f.blocked]); }
  let html = "<div class='dnote'>本地任务记录是唯一状态权威；状态变更需人工确认，" +
    '修订号防止旧读取覆盖他人改动。</div>';
  if (state === 'Merging') {
    html += "<div class='dnote warn'>worker 正在执行落地合并；已发出的 " +
      'GitHub 请求无法撤销，改态前请先确认。</div>';
  }
  html += "<div class='dgrid'>" + meta.map(function (kv) {
    return "<div class='dk'>" + esc(kv[0]) + "</div><div class='dv'>" + esc(kv[1]) + '</div>';
  }).join('') + '</div>';
  if (loading) {
    html += '<h3>源 Issue</h3><div class="dnote">加载中…</div>';
  } else if (sourceError) {
    html += '<h3>源 Issue</h3><div class="dnote warn">来源加载失败：' +
      esc(sourceError) + '；可关闭抽屉重新打开重试。</div>';
  } else if (source && source.issue) {
    html += '<h3>源 Issue</h3><div class="dnote">' + esc((source.issue.title || '') +
      (source.issue.state ? '（' + source.issue.state + '）' : '')) + '</div>';
    if (source.issue.body) {
      html += '<h3>任务描述</h3><pre class="dpre">' + esc(source.issue.body) + '</pre>';
    }
  }
  if (source && source.workpad) {
    html += '<h3>Workpad</h3><pre class="dpre">' + esc(source.workpad.body || '') + '</pre>';
  }
  if (source && source.pull_request) {
    const p = source.pull_request;
    html += '<h3>PR</h3><div class="dnote">#' + esc(p.number) + ' ' + esc(p.title || '') +
      ' · ' + esc(p.state || '') + (p.merged ? ' · 已合并' : '') + '</div>';
  }
  const hist = (source && source.task && source.task.history) || [];
  if (hist.length) {
    html += '<h3>活动</h3><div class="dhist">' + hist.slice().reverse().map(function (h) {
      const from = h.from ? esc(h.from) : '新建';
      const to = esc(h.to || '');
      const by = h.by ? esc(h.by) : '';
      return "<div class='dhrow'><span class='dhdot'></span>" +
        "<span class='dhmain'>" + from + ' → <b>' + to + '</b>' +
        (by ? " <span class='dhby'>· " + by + '</span>' : '') + '</span>' +
        "<span class='dhtime'>" + esc(fmtWhen(h.at)) + '</span></div>';
    }).join('') + '</div>';
  }
  html += '<h3>移动到</h3><div class="dactions">' + STATES.map(function (s) {
    const dis = (s === state) ? ' disabled' : '';
    return "<button class='dlg-btn' data-move='" + esc(s) + "'" + dis + '>' + esc(s) + '</button>';
  }).join('') + '</div>';
  html += '<h3>评论 / 说明</h3><textarea id="dcomment" rows="3" maxlength="4000" ' +
    "placeholder='会先发布评论，再执行状态变更（可只评论不改态）'></textarea>" +
    "<div class='dactions'><button class='dlg-btn' id='dcomment-btn'>仅发表评论</button></div>";
  if (AGENT) {
    html += '<h3>执行中</h3><div class="dnote">' + esc(AGENT.event || AGENT.message || '') +
      (AGENT.started_at ? ' · 开始 ' + esc(AGENT.started_at) : '') + '</div>';
  }
  if (TROUBLE && TROUBLE.error) {
    html += '<h3>最近错误</h3><pre class="dpre">' + esc(TROUBLE.error) + '</pre>';
  }
  html += '<h3>会话记录</h3>';
  html += RUNS.length ? RUNS.map(function (r) {
    return "<div class='drun" + (r.active ? ' live' : '') + "'>" +
      "<a href='/run/" + encodeURIComponent(CUR) + '/' + esc(r.short) + "'>" +
      esc(r.started) + ' · ' + esc(r.messages) + ' 条消息 · ' + esc(r.size) + '</a></div>';
  }).join('') : "<div class='dnote'>还没有运行记录</div>";
  dbody.innerHTML = html;
}

document.getElementById('dbody').addEventListener('click', function (e) {
  const moveBtn = e.target.closest('[data-move]');
  if (moveBtn && CUR) {
    const target = moveBtn.getAttribute('data-move');
    if (target === 'Merging' && !window.confirm(
        '进入 Merging 后 worker 将执行落地合并；已发出的 GitHub 请求无法撤销。确认？')) {
      return;
    }
    const comment = (document.getElementById('dcomment') || {}).value || '';
    submitMove(CUR, target, comment.trim());
    return;
  }
  if (e.target.closest('#dcomment-btn') && CUR) {
    const comment = (document.getElementById('dcomment') || {}).value || '';
    if (!comment.trim()) { showToast('warn', '评论不能为空'); return; }
    submitComment(CUR, comment.trim());
  }
});
document.getElementById('dhead').addEventListener('click', function (e) {
  if (e.target.closest('#dclose')) { closeDrawer(); }
});

function closeDrawer() {
  CUR = null;
  CUR_REV = null;
  CUR_SOURCE = null;
  CUR_SOURCE_ERR = null;
  OPEN_SEQ += 1;
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('shade').className = '';
}
document.getElementById('shade').addEventListener('click', closeDrawer);
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && CUR) { closeDrawer(); }
});

boardEl.addEventListener('click', function (e) {
  const qb = e.target.closest('.bqbtn');
  if (qb) {
    e.preventDefault();
    e.stopPropagation();
    submitMove(qb.getAttribute('data-issue'), qb.getAttribute('data-target'), '');
    return;
  }
  const card = e.target.closest('.bcard');
  if (card && !e.target.closest('a')) { openIssue(card.getAttribute('data-issue')); }
});

// --- 拖拽改状态：卡片拖到另一列即可流转 ---------------------------------
const COL_STATE = {
  backlog: 'Backlog', todo: 'Todo', in_progress: 'In Progress',
  human_review: 'Human Review', merging: 'Merging', rework: 'Rework', done: 'Done'
};

boardEl.addEventListener('dragstart', function (e) {
  const card = e.target.closest('.bcard');
  if (!card) { return; }
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', card.getAttribute('data-issue'));
  card.classList.add('dragging');
});
boardEl.addEventListener('dragend', function (e) {
  const card = e.target.closest('.bcard');
  if (card) { card.classList.remove('dragging'); }
  document.querySelectorAll('.col.drop-target').forEach(function (c) {
    c.classList.remove('drop-target');
  });
});

document.querySelectorAll('.col').forEach(function (col) {
  const lane = col.getAttribute('data-col');
  col.addEventListener('dragover', function (e) {
    if (!COL_STATE[lane]) { return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    col.classList.add('drop-target');
  });
  col.addEventListener('dragleave', function (e) {
    if (!col.contains(e.relatedTarget)) { col.classList.remove('drop-target'); }
  });
  col.addEventListener('drop', function (e) {
    e.preventDefault();
    col.classList.remove('drop-target');
    const issue = e.dataTransfer.getData('text/plain');
    const target = COL_STATE[lane];
    if (!issue || !target) { return; }
    const f = FACTS[issue] || {};
    if (f.state === target) { return; }
    submitMove(issue, target, '');
  });
});

function applyFilter() {
  const q = (document.getElementById('q').value || '').toLowerCase();
  COLS.forEach(function (k) {
    document.getElementById('col-' + k).querySelectorAll('.bcard').forEach(function (c) {
      c.hidden = !!(q && (c.getAttribute('data-search') || '').indexOf(q) < 0);
    });
  });
}
document.getElementById('q').addEventListener('input', function () {
  applyFilter();
  applyShowEmpty();
  syncUrl();
});

function applyShowEmpty() {
  COLS.forEach(function (k) {
    const col = document.querySelector(".col[data-col='" + k + "']");
    let visible = 0;
    col.querySelectorAll('.bcard').forEach(function (c) { if (!c.hidden) { visible += 1; } });
    const empty = !visible;
    col.classList.toggle('is-empty', empty);
    col.style.display = (SHOW_EMPTY || !empty) ? '' : 'none';
  });
}
document.getElementById('showEmpty').addEventListener('change', function (e) {
  SHOW_EMPTY = e.target.checked;
  applyShowEmpty();
  syncUrl();
});

// --- URL 同步：视图/搜索/空列写回地址栏，刷新后仍保留 -------------------
let CUR_VIEW = 'active';

function syncUrl() {
  const p = new URLSearchParams();
  if (CUR_VIEW !== 'active') { p.set('view', CUR_VIEW); }
  const q = (document.getElementById('q').value || '').trim();
  if (q) { p.set('q', q); }
  if (!SHOW_EMPTY) { p.set('empty', '0'); }
  const s = p.toString();
  history.replaceState(null, '', s ? '?' + s : location.pathname);
}

function restoreFromUrl() {
  const p = new URLSearchParams(location.search);
  const view = p.get('view');
  if (view && view !== CUR_VIEW) { switchView(view); }
  const q = p.get('q');
  if (q) {
    document.getElementById('q').value = q;
    applyFilter();
  }
  if (p.get('empty') === '0') {
    SHOW_EMPTY = false;
    document.getElementById('showEmpty').checked = false;
    applyShowEmpty();
  }
}

// --- 运行时长每秒刷新（卡片上的 data-since） ---------------------------
function fmtWhen(iso) {
  if (!iso) { return ''; }
  const t = Date.parse(iso);
  if (isNaN(t)) { return String(iso); }
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) { return '刚刚'; }
  if (s < 3600) { return Math.floor(s / 60) + ' 分钟前'; }
  if (s < 86400) { return Math.floor(s / 3600) + ' 小时前'; }
  const d = new Date(t);
  const pad = function (n) { return (n < 10 ? '0' : '') + n; };
  return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
    pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function fmtDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) { return h + 'h' + m + 'm'; }
  if (m) { return m + 'm' + s + 's'; }
  return s + 's';
}

setInterval(function () {
  document.querySelectorAll('[data-since]').forEach(function (el) {
    const started = Date.parse(el.getAttribute('data-since'));
    if (!isNaN(started)) { el.textContent = '已运行 ' + fmtDuration(Date.now() - started); }
  });
}, 1000);

const COMPLETED = {issues: [], today: '', loaded: false};
function switchView(view) {
  CUR_VIEW = view;
  document.querySelectorAll('[data-view]').forEach(function (b) {
    b.classList.toggle('on', b.getAttribute('data-view') === view);
  });
  const board = document.getElementById('board');
  const panel = document.getElementById('completed-panel');
  if (view === 'active') {
    board.hidden = false;
    panel.hidden = true;
  } else {
    board.hidden = true;
    panel.hidden = false;
    loadCompleted(true);
  }
  syncUrl();
}
document.querySelectorAll('[data-view]').forEach(function (b) {
  b.addEventListener('click', function () { switchView(b.getAttribute('data-view')); });
});

async function loadCompleted(force) {
  if (COMPLETED.loaded && !force) { return; }
  const status = document.getElementById('completed-status');
  try {
    const d = await (await fetch('/api/completed', {cache: 'no-store'})).json();
    COMPLETED.issues = d.issues || [];
    COMPLETED.today = d.today || '';
    COMPLETED.loaded = true;
    status.textContent = d.warning || '';
    renderCompleted();
  } catch (e) {
    status.textContent = '读取已完成任务失败';
  }
}
document.getElementById('completed-refresh').addEventListener('click', function () {
  COMPLETED.loaded = false;
  loadCompleted(true);
});
document.getElementById('cstate').addEventListener('change', renderCompleted);

function renderCompleted() {
  const view = document.querySelector('.chip.on[data-view]');
  const which = view ? view.getAttribute('data-view') : 'active';
  const list = document.getElementById('completed-list');
  const stateSel = document.getElementById('cstate');
  const wantedState = stateSel ? stateSel.value : '';
  const states = [];
  COMPLETED.issues.forEach(function (i) {
    const s = i.state || '';
    if (s && states.indexOf(s) < 0) { states.push(s); }
  });
  if (stateSel) {
    const current = wantedState;
    stateSel.innerHTML = "<option value=''>全部终止状态</option>" +
      states.map(function (s) {
        return "<option value='" + esc(s) + "'" +
          (s === current ? ' selected' : '') + '>' + esc(s) + '</option>';
      }).join('');
  }
  const items = COMPLETED.issues.filter(function (i) {
    if (wantedState && i.state !== wantedState) { return false; }
    return which === 'today' ? i.day === COMPLETED.today : true;
  });
  document.getElementById('completed-count').textContent =
    items.length + ' 条' + (which === 'today' ? '今日完成' : '历史完成');
  list.innerHTML = items.map(function (i) {
    const pr = i.merged_pr ? " · <a href='" + esc(i.merged_pr.url) +
      "' target='_blank' rel='noopener noreferrer'>PR #" + esc(i.merged_pr.number) + '</a>' : '';
    const issue = i.url ? "<a href='" + esc(i.url) + "' target='_blank' rel='noopener noreferrer'>GH-" +
      esc(i.number) + '</a>' : ('GH-' + esc(i.number));
    return "<div class='crow'><b>" + issue + '</b> ' + esc(i.title || '') +
      "<span class='cnote'>" + esc(i.state || '') + ' · ' + esc(i.completed_at || '') + pr +
      '</span></div>';
  }).join('') || "<div class='dnote'>暂无记录</div>";
}
setInterval(function () {
  if (!document.getElementById('completed-panel').hidden) { loadCompleted(false); }
}, 60000);

restoreFromUrl();
refreshIssues();
setInterval(refreshIssues, 15000);
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


_message_cache = {}
_message_lock = threading.Lock()


def count_messages(path, live):
    try:
        info = os.stat(path)
    except OSError:
        return 0
    key = (path, live)
    with _message_lock:
        cached = _message_cache.get(key)
        same_file = (
            cached
            and cached["dev"] == info.st_dev
            and cached["ino"] == info.st_ino
        )
        if same_file and cached["mtime_ns"] == info.st_mtime_ns and cached["size"] == info.st_size:
            return cached["count"]
        if live and same_file and cached["size"] < info.st_size:
            offset = cached["offset"]
            count = cached["count"]
        else:
            offset = 0
            count = 0
    try:
        with open(path, "rb") as handle:
            if offset:
                handle.seek(offset)
            if not live:
                count = sum(1 for line in handle if line.startswith(b'{"type":"message"'))
                end = handle.tell()
            else:
                end = offset
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        end = line_start
                        break
                    if b"entry_appended" in line:
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        entry = rec.get("entry") or {}
                        if rec.get("type") == "entry_appended" and entry.get("type") == "message":
                            count += 1
                    end = handle.tell()
    except OSError:
        return 0
    with _message_lock:
        _message_cache[key] = {
            "dev": info.st_dev,
            "ino": info.st_ino,
            "mtime_ns": info.st_mtime_ns,
            "size": info.st_size,
            "offset": end,
            "count": count,
        }
    return count


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


def load_runtime():
    """Configured runtime model/thinking level from the WORKFLOW frontmatter."""
    try:
        with open(WORKFLOW, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return {}
    model = MODEL_RE.search(text)
    thinking = THINKING_RE.search(text)
    return {
        "model": model.group(1) if model else "",
        "thinking": thinking.group(1) if thinking else "",
    }


def runtime_badge():
    rt = load_runtime()
    parts = []
    if rt.get("model"):
        parts.append("<b>%s</b>" % esc(rt["model"]))
    if rt.get("thinking"):
        parts.append("思考 %s" % esc(rt["thinking"]))
    if not parts:
        return ""
    return "<div class='runtime' title='WORKFLOW 配置的 runtime 模型与思考级别'>%s</div>" % " · ".join(parts)


def live_model(path, limit=65536):
    """Best-effort: the first assistant message_start in a live log carries provider/model."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(limit)
    except OSError:
        return "", ""
    for line in head.split(b"\n")[:400]:
        if b'"message_start"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") or {}
        if message.get("role") == "assistant" and message.get("model"):
            return message.get("provider") or "", message.get("model") or ""
    return "", ""


# Board reads/writes go through the scheduler's task API over loopback;
# the board never holds a GitHub token and never calls api.github.com.
API_TOKEN = os.environ.get("SYMPHONY_API_TOKEN", "")


def scheduler_url(path):
    parts = urlparse(STATE_URL)
    return parts._replace(path=path, query="", fragment="").geturl()


def scheduler_request(method, path, body=None, timeout=10):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = "Bearer %s" % API_TOKEN
    request = urllib.request.Request(
        scheduler_url(path), data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


_tasks_cache = {"data": None, "at": 0.0}


def fetch_tasks():
    try:
        payload = scheduler_request("GET", "/api/v1/tasks", timeout=5)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, http.client.HTTPException):
        return None
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    return tasks if isinstance(tasks, list) else None


def board_tasks():
    if (_tasks_cache["data"] is not None
            and time.time() - _tasks_cache["at"] < ISSUE_CACHE_TTL):
        return _tasks_cache["data"]
    tasks = fetch_tasks()
    if tasks is not None:
        _tasks_cache["data"] = tasks
        _tasks_cache["at"] = time.time()
    return _tasks_cache["data"]


_source_cache = {}
_source_lock = threading.Lock()
SOURCE_TTL = 30.0


def invalidate_task_source(number):
    with _source_lock:
        _source_cache.pop(number, None)


def fetch_task_source(number):
    # The scheduler proxies GitHub calls (issue + comment pages + optional PR);
    # they now run concurrently there, but a repeat open of the same task is
    # still a full network round trip. Cache briefly and drop the entry on any
    # local write so the drawer never shows stale workflow state.
    now = time.time()
    with _source_lock:
        hit = _source_cache.get(number)
        if hit and now - hit[0] < SOURCE_TTL:
            return hit[1]
    try:
        payload = scheduler_request("GET", "/api/v1/tasks/%d/source" % number,
                                    timeout=45)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError, http.client.HTTPException):
        return None
    if isinstance(payload, dict):
        with _source_lock:
            _source_cache[number] = (time.time(), payload)
    return payload


def request_task_comment(number, body):
    return scheduler_request(
        "POST", "/api/v1/tasks/%d/comments" % number, {"body": body}, timeout=30)


def request_task_patch(number, fields, expected_revision):
    return scheduler_request(
        "PATCH", "/api/v1/tasks/%d" % number,
        {**fields, "expected_revision": expected_revision}, timeout=15)


def repo_of(tasks):
    for record in tasks or []:
        if record.get("repo"):
            return record["repo"]
    return ""


def clear_board_cache():
    _tasks_cache["data"] = None
    _tasks_cache["at"] = 0.0


def request_scheduler_refresh():
    """Best effort: a tracker write remains successful if refresh is unavailable."""
    parts = urlparse(STATE_URL)
    url = parts._replace(path="/api/v1/refresh", query="", fragment="").geturl()
    try:
        request = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=2) as response:
            return bool(json.load(response).get("queued"))
    except (OSError, ValueError, http.client.HTTPException):
        return False


_state_cache = {"data": None, "at": 0.0, "started": False}
_state_lock = threading.Lock()


def fetch_state():
    try:
        with urllib.request.urlopen(STATE_URL, timeout=3) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None


def symphony_state():
    with _state_lock:
        data = _state_cache["data"]
        started = _state_cache["started"]
    if data is not None or started:
        return data
    return fetch_state()


def state_sync_error():
    with _state_lock:
        failed = _state_cache.get("failed", False)
        at = _state_cache.get("at", 0)
        stale = _state_cache.get("started") and (not at or time.time() - at > 15)
    return "宿主同步异常 · 保留上次状态，暂停批准操作" if failed or stale else ""


def state_poller():
    announced = False
    while True:
        observed_after = time.time()
        data = fetch_state()
        if data is not None:
            with _state_lock:
                _state_cache["data"] = data
                _state_cache["at"] = time.time()
                _state_cache["observed_after"] = observed_after
                _state_cache["failed"] = False
            if not announced:
                print("state poller ready: %s" % STATE_URL, flush=True)
                announced = True
        else:
            with _state_lock:
                _state_cache["failed"] = True
        time.sleep(STATE_POLL_INTERVAL)


def tasks_poller():
    """Keeps task/session caches warm so request threads never block on the scheduler."""
    announced = False
    while True:
        started = time.time()
        try:
            tasks = fetch_tasks()
            if tasks is not None:
                _tasks_cache["data"] = tasks
                _tasks_cache["at"] = time.time()
            scan_sessions()
        except Exception:
            pass
        if not announced:
            print("tasks poller ready", flush=True)
            announced = True
        time.sleep(max(1.0, GITHUB_POLL_INTERVAL - (time.time() - started)))


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


def session_active(run, agent, state, now=None):
    """Only associate an active worker with its own recorded session."""
    if state is not None:
        if not agent:
            return False
        session_id = agent.get("session_id")
        return bool(session_id and session_id == run.get("id"))
    return (time.time() if now is None else now) - run["mtime"] < LIVE_WINDOW


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
                    "delay_type": entry.get("delay_type") or "",
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
    "idle": ("未派发", "idle"),
    "ready": ("待领取", "ready"),
    "running": ("进行中", "running"),
    "retrying": ("重试中", "retrying"),
    "blocked": ("已暂停", "blocked"),
    "awaiting": ("待审核", "awaiting"),
    "done": ("已完成", "done"),
}

# Board columns are the seven local workflow states. Terminal aliases
# (Closed/Cancelled/Canceled/Duplicate) share the Done lane. Blocked/retrying
# stay runtime badges on cards, never columns.
COLUMNS = [
    ("backlog", "Backlog", "未加入执行队列"),
    ("todo", "Todo", "等待调度器领取"),
    ("in_progress", "In Progress", "执行与重试"),
    ("human_review", "Human Review", "人工审核 · 非活跃状态"),
    ("merging", "Merging", "worker 执行 land 合并"),
    ("rework", "Rework", "退回重做"),
    ("done", "Done", "已结束的任务"),
]

BOARD_STATES = {
    "Backlog", "Todo", "In Progress", "Human Review", "Merging",
    "Rework", "Done", "Closed", "Cancelled", "Canceled", "Duplicate",
}

# 卡片快捷操作：只暴露人工决策的下一步，避免把调度器职责搬到界面上。
QUICK_NEXT = {
    "Backlog": (("Todo", "→ Todo"),),
    "In Progress": (("Human Review", "→ 审核"),),
    "Human Review": (("Merging", "→ 合并"),),
    "Rework": (("In Progress", "→ 继续"),),
}
QUICK_BACK = {
    "In Progress": ("Rework", "退回"),
    "Human Review": ("Rework", "退回"),
}

STATE_TO_LANE = {
    "Backlog": "backlog",
    "Todo": "todo",
    "In Progress": "in_progress",
    "Human Review": "human_review",
    "Merging": "merging",
    "Rework": "rework",
    "Done": "done",
    "Closed": "done",
    "Cancelled": "done",
    "Canceled": "done",
    "Duplicate": "done",
}


def lane_of_state(state):
    return STATE_TO_LANE.get(state or "Backlog", "backlog")


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
    "done": (
        '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" '
        'stroke="currentColor" stroke-width="1.6"/><path d="M5.6 8.2l1.7 1.7 3.2-3.5" '
        'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg>'
    ),
}

EXT_ICON = (
    '<svg viewBox="0 0 16 16"><path fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round" d="M6.5 3.5h-3v9h9v-3M9.5 3.5h3v3M12 4 8 8"/></svg>'
)


def task_card_state(record, agent):
    """Local task record + runtime badge -> (tone, text). Local state only."""
    state = (record or {}).get("state") or "Backlog"
    if agent == "running":
        return "running", "AI 执行中"
    if agent == "retrying":
        return "retrying", "重试中"
    if agent == "blocked":
        return "blocked", "执行受阻 · 等待重试"
    text = {
        "Backlog": "未派发",
        "Todo": "等待调度器领取",
        "In Progress": "执行中 · 等待会话上线",
        "Human Review": "等待人工审核",
        "Merging": "等待落地合并",
        "Rework": "退回重做",
        "Done": "已完成",
        "Closed": "已关闭",
        "Cancelled": "已取消",
        "Canceled": "已取消",
        "Duplicate": "重复任务",
    }.get(state, state)
    lane = lane_of_state(state)
    tone = {
        "backlog": "idle",
        "todo": "ready",
        "in_progress": "ready",
        "human_review": "awaiting",
        "merging": "ready",
        "rework": "retrying",
        "done": "done",
    }.get(lane, "idle")
    return tone, text


def build_cards():
    tasks = board_tasks()
    if tasks is None:
        return None
    sessions = scan_sessions()
    state = symphony_state()
    status = agent_status(state)
    agents = agent_detail(state)
    errors = agent_errors(state)

    rendered = []
    facts = {}
    counts = {key: 0 for key, _label, _sub in COLUMNS}
    repo = repo_of(tasks)
    done_budget = DONE_COL_LIMIT

    for record in tasks:
        number = record.get("issue_number")
        name = record.get("identifier") or ("GH-%s" % number)
        entries = sessions.get(name) or []
        labels = record.get("labels") or []
        agent = status.get(name)
        st, state_text = task_card_state(record, agent)
        lane = lane_of_state(record.get("state"))
        if lane == "done" and done_budget <= 0:
            continue
        counts[lane] = counts.get(lane, 0) + 1
        source_closed = (record.get("native_state") or "open") == "closed"

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
            right = "<span class='bright'>%d 个会话 · %s</span>" % (runs, esc(latest[5:16]))
        else:
            right = "<span class='bright'>未运行</span>"

        err_html = ""
        if trouble.get("error"):
            full = trouble["error"]
            err_html = "<div class='berr' title='%s'>%s</div>" % (esc(full), esc(short_error(full)))

        title = record.get("title") or ""
        gen = "<span class='bgen'>%d 个会话</span>" % runs if runs else ""
        facts[name] = {
            "state": record.get("state") or "Backlog",
            "rev": record.get("revision"),
            "closed": source_closed,
            "labels": sorted(str(l).lower() for l in labels),
            "branch": record.get("branch_name") or "",
            "workpad": record.get("workpad_comment_id"),
            "blocked": record.get("blocked_reason") or "",
        }
        if source_closed:
            state_text += " · 源已关闭"
        if lane == "done":
            done_budget -= 1
        pr_html = ""
        pr_url = record.get("pr_url") or ""
        pr_number = record.get("pr_number")
        if pr_url and pr_number:
            pr_html = ("<a class='bpr' href='%s' target='_blank' rel='noopener noreferrer'>PR #%s</a>"
                       % (esc(pr_url), esc(pr_number)))
        search = ("%s %s %s" % (name, title, repo or "")).lower()
        task_state = record.get("state") or "Backlog"
        quick = ""
        for target, label in QUICK_NEXT.get(task_state, ()):
            quick += ("<button class='bqbtn' data-issue='%s' data-target='%s'>%s</button>"
                      % (esc(name), esc(target), esc(label)))
        back = QUICK_BACK.get(task_state)
        if back:
            quick += ("<button class='bqbtn back' data-issue='%s' data-target='%s'>%s</button>"
                      % (esc(name), esc(back[0]), esc(back[1])))
        quick = "<div class='bquick'>%s</div>" % quick if quick else ""
        rendered.append(
            {
                "state": st,
                "lane": lane,
                "runs": runs,
                "updated": record.get("updated_at") or "",
                "html": (
                    "<div class='bcard %(state)s%(live)s' data-issue='%(name)s' draggable='true' "
                    "data-title='%(title)s' data-url='%(url)s' data-state='%(state)s' "
                    "data-state-text='%(text)s' data-task-state='%(task_state)s' "
                    "data-rev='%(rev)s' data-source-state='%(native)s' "
                    "data-pr='%(prurl)s' data-pr-num='%(prnum)s' data-search='%(search)s'>"
                    "<div class='btitle'>%(title)s</div>"
                    "<div class='bmeta'>"
                    "<div class='brow'><span class='bid'>%(name)s</span>%(gen)s%(pr)s"
                    "<a class='ext' href='%(url)s' target='_blank' rel='noopener noreferrer' "
                    "title='在 GitHub 打开'>%(ext)s</a></div>"
                    "<div class='brow'><span class='bstat tone-%(state)s'>%(icon)s%(text)s</span>"
                    "%(right)s</div>"
                    "</div>%(err)s%(quick)s</div>"
                )
                % {
                    "state": esc(st),
                    "live": " live" if st == "running" else "",
                    "name": esc(name),
                    "title": esc(title),
                    "url": esc(record.get("url") or ""),
                    "text": esc(state_text),
                    "task_state": esc(record.get("state") or "Backlog"),
                    "rev": esc(record.get("revision") if record.get("revision") is not None else ""),
                    "native": esc(record.get("native_state") or "open"),
                    "prurl": esc(pr_url),
                    "prnum": esc(pr_number or ""),
                    "search": esc(search),
                    "gen": gen,
                    "ext": EXT_ICON,
                    "icon": ICONS[st],
                    "pr": pr_html,
                    "right": right,
                    "err": err_html,
                    "quick": quick,
                },
            }
        )

    rendered.sort(key=lambda r: r["updated"], reverse=True)
    with_runs = sum(1 for e in rendered if e["runs"])
    cols = {}
    for key, _label, _sub in COLUMNS:
        cols[key] = "".join(r["html"] for r in rendered if r["lane"] == key)
    return {
        "cols": cols,
        "counts": counts,
        "total": len(tasks),
        "with_runs": with_runs,
        "repo": repo or "-",
        "writable": True,
        "facts": facts,
    }


def issues_page():
    data = {"counts": {}, "cols": {}, "total": 0, "writable": True,
            "repo": repo_of(board_tasks() or []) or "-"}
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
            "<span>GitHub 不可用 · 仅显示本地记录</span></div>%s</header>"
            "<div class='head'><div class='hrow'><h1>Issue 工作台</h1>"
            "<span class='sum'>%d 个有记录的 issue</span></div>"
            "<div class='note'>GitHub issues unavailable; showing recorded runs only · <a href='/history'>今日完成 / 历史记录</a></div></div>"
            "<div class='tools'><div class='chips'><span class='chip on'>全部 <b>%d</b></span></div></div>"
            "<main class='board' id='board'>%s</main></div>"
            % (ACTIVITY_ICON, runtime_badge(), len(sessions), len(sessions),
               "".join(cards) or "<div class='empty-col'>no runs recorded yet</div>")
        )
        return page("Symphony", body)

    counts = data["counts"]
    summary = " · ".join(
        "%d %s" % (counts.get(key, 0), label)
        for key, label, _sub in COLUMNS
        if counts.get(key, 0)
    ) or "正在加载任务…"
    col_html = []
    for key, label, sub in COLUMNS:
        cards = data["cols"].get(key) or ""
        inner = cards or "<div class='empty-col'>正在加载…</div>"
        col_html.append(
            "<section class='col%s' data-col='%s'>"
            "<header class='col-head'><span class='cdot %s'></span>"
            "<div class='chead'><div class='cline'><span class='clabel'>%s</span>"
            "<span class='ccount' id='cc-%s'>%s</span></div>"
            "<div class='csub'>%s</div></div></header>"
            "<div class='col-body' id='col-%s'>%s</div></section>"
            % ("" if cards else " is-empty", key, key, label, key, "—", sub, key, inner)
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
        "<span id='health'>正在加载数据…</span></div>"
        "%s"
        "<div class='auto'>%s<span>自动刷新 15 秒</span>"
        "<a href='%s' target='_blank' rel='noopener noreferrer'>%s</a></div>"
        "</header>"
        "<div class='head'><div class='hrow'><h1>Issue 工作台</h1>"
        "<span class='sum' id='sum'>%s</span></div>"
        "<div class='note'>业务状态来自任务记录与本地运行状态（最近一次对账）；"
        "点击卡片可查看历次运行、实时日志，并直接操作任务状态（Running/Retrying 等执行状态只记录在本机）。</div></div>"
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
        "<select class='chip' id='cstate'><option value=''>全部终止状态</option></select>"
        "<button class='chip' id='completed-refresh'>刷新</button></div>"
        "<p class='completed-note'>按本地任务记录的终止状态（Done/Cancelled/Closed/Duplicate）展示，点击卡片在当前页查看。</p>"
        "<p id='completed-status' role='status' aria-live='polite'></p>"
        "<div class='completed-list' id='completed-list'></div></main>"
        "</div>"
        "<div id='shade' onclick='closeDrawer()'></div>"
        "<div id='toast' role='status' aria-live='polite'>"
        "<span class='ticon' aria-hidden='true'></span><span class='tmsg'></span></div>"
        "<div id='modal' aria-hidden='true'>"
        "<div class='dialog' role='dialog' aria-modal='true' aria-labelledby='dlg-title' aria-describedby='dlg-body'>"
        "<div class='dlg-head'><span class='dlg-icon info' id='dlg-icon' aria-hidden='true'></span>"
        "<div class='dlg-copy'><h2 class='dlg-title' id='dlg-title'></h2>"
        "<p class='dlg-body' id='dlg-body'></p></div></div>"
        "<div class='dlg-field' id='dlg-field' hidden>"
        "<textarea id='dlg-input' rows='3' maxlength='4000' spellcheck='false'></textarea>"
        "<span class='dlg-count' id='dlg-count'></span></div>"
        "<div class='dlg-actions'><button type='button' class='dlg-btn' id='dlg-cancel'>取消</button>"
        "<button type='button' class='dlg-btn primary' id='dlg-ok'>确认</button></div>"
        "</div></div>"
        "<aside class='drawer' id='drawer'><div class='dgrip' id='dgrip' title='拖动调整宽度'></div>"
        "<div class='dhead' id='dhead'></div>"
        "<div class='dbody' id='dbody'></div></aside>"
    ) % (
        ACTIVITY_ICON,
        runtime_badge(),
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


def completed_data():
    """Terminal tasks from the local records (Done/Closed/Cancelled/Duplicate)."""
    tasks = board_tasks() or []
    today = datetime.now(CN).date().isoformat()
    issues = []
    for record in tasks:
        if lane_of_state(record.get("state")) != "done":
            continue
        ts = parse_ts(record.get("updated_at") or "")
        if ts is None:
            continue
        issues.append({
            "number": record.get("issue_number"),
            "title": record.get("title") or "",
            "at": record.get("updated_at"),
            "url": record.get("url") or "",
            "state": record.get("state"),
            "day": ts.astimezone(CN).date().isoformat(),
            "completed_at": fmt_dt(record.get("updated_at") or ""),
            "merged_pr": (
                {"number": record.get("pr_number"), "url": record.get("pr_url")}
                if record.get("pr_url") else None),
        })
    issues.sort(key=lambda e: e["at"] or "", reverse=True)
    return {"issues": issues, "today": today, "warning": ""}


def issue_page(name):
    sessions = scan_sessions()
    entries = sessions.get(name) or []
    record = next((r for r in (board_tasks() or [])
                   if r.get("identifier") == name), {})
    tone, _state_text = task_card_state(
        record, agent_status(symphony_state()).get(name))
    label_text, badge_cls = STATUS_UI.get(tone, STATUS_UI["idle"])

    if not entries:
        header = (
            "<h1>%s <span class='badge %s'>%s</span></h1>"
            "<div class='meta'><a href='/'>返回全部 issue</a> &middot; "
            "<a href='/live/%s'>实时视图</a></div>"
            % (esc(name), esc(badge_cls), esc(label_text), esc(name))
        )
        return page(name, header + "<div class='empty'>该 issue 还没有运行记录</div>")

    cards = []
    total_msgs = 0
    for position, run in enumerate(reversed(entries), 1):
        href = "/run/%s/%s" % (name, run["short"])
        latest = "<span class='badge ready'>最新</span>" if position == 1 else ""
        total_msgs += run["messages"]
        live_dot = "live" if run.get("live") else "idle"
        cards.append(
            "<a class='runrow' href='%s'>"
            "<span class='dot %s'></span>"
            "<span class='rr-main'><span class='rr-time'>%s%s</span>"
            "<span class='rr-sub'>%d 条消息 · %s · %s</span></span>"
            "<span class='rr-go'>›</span></a>"
            % (
                esc(href),
                live_dot,
                esc(run["started"]),
                latest,
                run["messages"],
                human_size(run["size"]),
                esc(run["short"]),
            )
        )

    header = (
        "<h1>%s <span class='badge %s'>%s</span></h1>"
        "<div class='meta'><a href='/'>返回全部 issue</a> &middot; "
        "<a href='/live/%s'>实时视图</a></div>"
        "<div class='runbar'><span><b>%d</b> 次运行</span>"
        "<span><b>%d</b> 条消息</span>"
        "<span>最近 <b>%s</b></span></div>"
        % (esc(name), esc(badge_cls), esc(label_text), esc(name),
           len(entries), total_msgs, esc(entries[-1]["started"]))
    )
    return page(name, header + "<div class='runlist'>" + "".join(cards) + "</div>")


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
            label = ("[%s] " % stamp) if stamp else ""
            lines.append("<div class='ln mark'>%s用户</div>" % label)
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
    return "<div class='msg' data-role='%s'>%s</div>" % (esc(role or ''), "".join(lines))


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
            "<div class='msg' data-role='meta' data-bid='%s'><div class='ln mark err'>%s</div></div>"
            % (esc(bid), block.get("text", ""))
        )
    if kind == "tool":
        err = block.get("err")
        state = "" if block.get("done") else " · 执行中"
        tm = ("✗ " if err else "✓ ") + esc(block.get("time", "")) + state
        out = block.get("out") or ""
        return (
            "<div class='msg' data-role='tool' data-bid='%s'><div class='ln tool%s'>"
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
    return "<div class='msg' data-role='assistant' data-bid='%s'>%s</div>" % (esc(bid), "".join(parts))


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
    current = symphony_state()
    alive = session_active(run, agent_detail(current).get(issue), current)
    state = "live" if alive else "idle"

    body = """
<h1>%(issue)s <span id='status' class='%(state)s'>%(label)s</span></h1>
<div class='meta'>%(started)s · %(short)s · <a href='/issue/%(issue)s'>runs</a> · <a href='/'>all issues</a><span id='trim'></span></div>
<div class='tools'>
  <button class='chip on' id='follow'>跟随最新</button>
  <button class='chip on' data-filter='all'>全部</button>
  <button class='chip' data-filter='assistant'>助手</button>
  <button class='chip' data-filter='tool'>工具</button>
  <button class='chip' data-filter='user'>用户</button>
  <span class='grow'></span>
  <span class='meta' id='counter'></span>
</div>
<div id='detail' class='meta'></div>
<div id='feed'>%(fragment)s</div>
<script>
const ISSUE = %(issue_json)s;
const SRC = %(src_json)s;
const MAX_MSGS = 400;
let offset = %(offset)d;
let AGENT = null;
let skipped = 0;
let FOLLOW = true;
let FILTER = 'all';

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

function collapseLong(root) {
  (root || document).querySelectorAll('.msg').forEach(function (m) {
    if (m.dataset.collapseChecked) { return; }
    m.dataset.collapseChecked = '1';
    const targets = m.querySelectorAll('.ln.say, .ln.user, .ln.tool .out');
    let tall = false;
    targets.forEach(function (el) { if (el.scrollHeight > 320) { tall = true; } });
    if (!tall) { return; }
    m.classList.add('collapsed');
    const btn = document.createElement('button');
    btn.className = 'expand';
    btn.textContent = '展开全文';
    btn.addEventListener('click', function () {
      const open = m.classList.toggle('collapsed');
      btn.textContent = open ? '展开全文' : '收起';
    });
    m.appendChild(btn);
  });
}

function applyFilter() {
  const feed = document.getElementById('feed');
  if (!feed) { return; }
  let shown = 0;
  feed.querySelectorAll('.msg').forEach(function (m) {
    const role = m.getAttribute('data-role') || '';
    let keep = true;
    if (FILTER === 'assistant') { keep = role === 'assistant'; }
    else if (FILTER === 'tool') { keep = role === 'tool' || !!m.querySelector('.ln.tool'); }
    else if (FILTER === 'user') { keep = role === 'user'; }
    m.hidden = !keep;
    if (keep) { shown += 1; }
  });
  const c = document.getElementById('counter');
  if (c) { c.textContent = shown + ' / ' + feed.querySelectorAll('.msg').length + ' 条'; }
}

document.querySelectorAll('[data-filter]').forEach(function (b) {
  b.addEventListener('click', function () {
    FILTER = b.getAttribute('data-filter');
    document.querySelectorAll('[data-filter]').forEach(function (x) {
      x.classList.toggle('on', x === b);
    });
    applyFilter();
  });
});
document.getElementById('follow').addEventListener('click', function (e) {
  FOLLOW = !FOLLOW;
  e.target.classList.toggle('on', FOLLOW);
  if (FOLLOW) { window.scrollTo(0, document.body.scrollHeight); }
});

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
      feed.insertAdjacentHTML('beforeend', data.html);
      trimFeed();
      collapseLong(feed);
      applyFilter();
      if (FOLLOW) { window.scrollTo(0, document.body.scrollHeight); }
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
collapseLong();
applyFilter();
if (FOLLOW) { window.scrollTo(0, document.body.scrollHeight); }
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

        if path.startswith("/api/source/"):
            number = self.issue_number(path[len("/api/source/"):])
            if number is None:
                return self.send_json(400, {"error": "bad issue name"})
            payload = fetch_task_source(number)
            if payload is None:
                return self.send_json(502, {"error": "source unavailable"})
            return self.send_json(200, payload)

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
        self._completed_writes = 0
        self._mutation_outcome = "not_applied"
        parsed = urlparse(self.path)
        try:
            self.route_post(parsed)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the connection
            try:
                self.send_json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            except OSError:
                pass

    def route_post(self, parsed):
        if parsed.path == "/api/task":
            if not self.write_guard():
                return
            if not _task_lock.acquire(blocking=False):
                return self.send_json(409, {"error": "同一操作正在处理，请稍候核对结果"})
            try:
                return self.handle_task()
            finally:
                _task_lock.release()
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
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_json(400, {"error": "请求体不是合法 JSON"})
            return None
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "请求体必须是 JSON 对象"})
            return None
        return payload

    def issue_number(self, value):
        issue = str(value or "")
        if not ISSUE_RE.match(issue) or not issue.startswith(IDENTIFIER_PREFIX):
            return None
        number_text = issue[len(IDENTIFIER_PREFIX):]
        return int(number_text) if number_text.isdigit() else None

    def handle_task(self):
        """Board ops: comment-first then PATCH state with expected_revision.

        ``op`` is ``move`` (optional comment, then state change) or
        ``comment`` (comment only). The scheduler owns the task record; the
        board validates shape and forwards over loopback.
        """
        if not self.write_guard():
            return
        payload = self.read_json_body()
        if payload is None:
            return
        op = str(payload.get("op") or "")
        if op not in ("move", "comment"):
            return self.send_json(400, {"error": "op 必须是 move/comment"})
        number = self.issue_number(payload.get("issue"))
        if number is None:
            return self.send_json(400, {"error": "issue 标识不合法"})
        comment = str(payload.get("comment") or "").strip()
        if len(comment) > 4000:
            return self.send_json(400, {"error": "说明过长（上限 4000 字）"})
        target = str(payload.get("state") or "").strip()
        if op == "move" and target not in BOARD_STATES:
            return self.send_json(400, {"error": "目标状态不合法"})
        if op == "comment" and not comment:
            return self.send_json(400, {"error": "评论不能为空"})
        try:
            if comment:
                request_task_comment(number, comment)
                self._completed_writes += 1
            record = None
            if op == "move":
                record = request_task_patch(
                    number, {"state": target}, payload.get("expected_revision"))
                self._completed_writes += 1
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 403, 409}:
                code = ""
                try:
                    detail = json.loads(exc.read() or b"{}")
                    code = ((detail.get("error") or {}).get("code")) or ""
                except (ValueError, AttributeError):
                    code = ""
                hint = "任务状态已变化，请刷新核对后重新决定" if exc.code == 409 \
                    else "操作被拒绝，请核对任务状态"
                return self.send_json(exc.code, {
                    "error": hint, "code": code,
                    "comment_posted": bool(self._completed_writes)})
            self._mutation_outcome = "partial" if self._completed_writes else "uncertain"
            return self.send_json(502, {"error": "操作结果待核对，请勿重复提交",
                                        "comment_posted": bool(self._completed_writes)})
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            self._mutation_outcome = "partial" if self._completed_writes else "uncertain"
            return self.send_json(502, {"error": "操作结果待核对，请勿重复提交",
                                        "comment_posted": bool(self._completed_writes)})
        clear_board_cache()
        invalidate_task_source(number)
        refresh_queued = request_scheduler_refresh()
        task = (record or {}).get("task") or {}
        return self.send_json(200, {
            "ok": True,
            "message": "已移动到 %s" % target if op == "move" else "评论已记录",
            "state": task.get("state"),
            "revision": task.get("revision"),
            "refresh_queued": refresh_queued,
        })

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

        state = symphony_state()
        entry = agent_detail(state).get(issue)
        running = session_active(run, entry, state)
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
            return self.send_json(503, {"error": "scheduler unavailable"})
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
                "counts": {"total": data["total"], "with_runs": data["with_runs"],
                           **{k: counts.get(k, 0) for k, _l, _s in COLUMNS}},
                "repo": data["repo"],
                "writable": bool(data.get("writable")),
                "runtime": agent_status(symphony_state()),
                "facts": data.get("facts", {}),
                "state_observed_after": _state_cache.get("observed_after", 0),
                "sync_error": state_sync_error(),
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
            active = session_active(r, agent, state, now)
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
            provider, model = live_model(latest["path"]) if latest and latest.get("live") else ("", "")
            rt = load_runtime()
            agent_info = {
                "message": agent.get("last_message") or "",
                "event": agent.get("last_event") or "",
                "event_at": agent.get("last_event_at") or "",
                "started_at": agent.get("started_at") or "",
                "tokens": tokens.get("total_tokens"),
                "model": "%s/%s" % (provider, model) if provider and model else model or rt.get("model") or "",
                "thinking": rt.get("thinking") or "",
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

        state = symphony_state()
        active = session_active(run, agent_detail(state).get(issue), state)

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
        if status >= 400 and self.command == "POST" and urlparse(self.path).path in ("/api/task",):
            outcome = "partial" if getattr(self, "_completed_writes", 0) else getattr(self, "_mutation_outcome", "not_applied")
            data = {**data, "outcome": outcome}
            if data["outcome"] != "not_applied" or getattr(self, "_completed_writes", 0):
                clear_board_cache()
                request_scheduler_refresh()
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

    with _state_lock:
        _state_cache["started"] = True
    threading.Thread(target=state_poller, daemon=True, name="symphony-state").start()
    threading.Thread(target=tasks_poller, daemon=True, name="symphony-tasks").start()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.daemon_threads = True
    print("board on http://%s:%d/" % (args.bind, args.port), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

"""SSH transport for remote workers.

Mirrors ``SymphonyElixir.SSH``: OpenSSH is invoked with ``-T``, an optional
``-F`` config, an optional ``-p`` port parsed from ``host:port`` shorthand,
and ``bash -lc '<command>'`` on the remote side.  Remote MCP access uses an
OpenSSH reverse forward so the worker never needs to reach the host loopback
directly.
"""

import os
import re
import shutil
import subprocess


class SSHError(Exception):
    """Transport failure with a machine-readable ``reason``."""

    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        text = reason if detail is None else "%s: %s" % (reason, detail)
        super().__init__(text)


_TARGET_PORT_RE = re.compile(r"^(.*):(\d+)$")


def executable():
    path = shutil.which("ssh")
    if not path:
        raise SSHError("ssh_not_found")
    return path


def parse_target(target):
    """Split ``host:port`` shorthand without breaking IPv6 literals."""
    if not isinstance(target, str):
        return "", None
    trimmed = target.strip()
    match = _TARGET_PORT_RE.match(trimmed)
    if match is not None:
        destination, port = match.group(1), match.group(2)
        if _valid_port_destination(destination):
            return destination, port
    return trimmed, None


def _valid_port_destination(destination):
    if not destination:
        return False
    return ":" not in destination or (
        "[" in destination and "]" in destination)


def shell_escape(value):
    """Single-quote a value for a POSIX shell."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def remote_shell_command(command):
    return "bash -lc " + shell_escape(command)


def ssh_args(host, command, remote_forwards=None,
             exit_on_forward_failure=False):
    destination, port = parse_target(host)
    args = []
    config = os.environ.get("SYMPHONY_SSH_CONFIG")
    if config:
        args += ["-F", config]
    args.append("-T")
    if port:
        args += ["-p", port]
    if exit_on_forward_failure:
        args += ["-o", "ExitOnForwardFailure=yes"]
    for remote_port, local_host, local_port in remote_forwards or []:
        args += ["-R", "127.0.0.1:%s:%s:%s" % (
            remote_port, local_host, local_port)]
    args += [destination, remote_shell_command(command)]
    return args


def run(host, command, timeout_s=None):
    """Run one remote command and return ``(output, status)``."""
    if not isinstance(host, str) or host.strip() == "":
        raise SSHError("ssh_host_missing")
    ssh = executable()
    try:
        result = subprocess.run(
            [ssh] + ssh_args(host, command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SSHError("ssh_command_timeout", timeout_s) from error
    except OSError as error:
        raise SSHError("ssh_command_failed", str(error)) from error
    return result.stdout, result.returncode


def start_port(host, command, remote_forwards=None):
    """Start a long-lived SSH process with piped stdin/stdout."""
    if not isinstance(host, str) or host.strip() == "":
        raise SSHError("ssh_host_missing")
    ssh = executable()
    try:
        return subprocess.Popen(
            [ssh] + ssh_args(
                host, command, remote_forwards=remote_forwards,
                exit_on_forward_failure=bool(remote_forwards)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as error:
        raise SSHError("ssh_command_failed", str(error)) from error

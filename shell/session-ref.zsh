# Source from an interactive zsh. Resolve the checkout at load time, the CLI at launch.
typeset -g _SESSION_REF_LAUNCHER="${${(%):-%x}:A:h:h}/session-ref"
function session-ref { "$_SESSION_REF_LAUNCHER" "$@"; }
function codex { session-ref run codex "$@"; }
function claude { session-ref run claude "$@"; }
function agy { session-ref run agy "$@"; }

#!/bin/bash
# Prepare a follow-the-fake worktree: right accounts, workflow hooks, cloud config, and a
# short report. Run by Orca after creating a worktree (orca.yaml, scripts.setup) or by hand:
#   bash scripts/orca-setup.sh
# Idempotent, no installs, no compute, never prints a secret, never touches global config.
# Exit: 0 = ready (warnings allowed); 1 = an essential check failed (git identity, push
# credentials, hooks, direnv/.envrc, gcloud configuration); 2 = refused (not a worktree of
# visionary-sender/follow-the-fake), nothing changed.
# If the visionary-sender gh token is unavailable it only checks and changes nothing.
# See scripts/README.md. Compatible with macOS /bin/bash 3.2.

REPO_SLUG="visionary-sender/follow-the-fake"
GH_USER="visionary-sender"
LINEAR_WS="e00855b4-5a23-4118-b13f-69b62b20dc35"
GCLOUD_CONF="follow-the-fake"
GCLOUD_PROJECT="follow-the-fake"
SKILL_DIR="$HOME/.agents/skills/follow-the-fake-workflow"
LINEAR_TIMEOUT=8

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_FAIL=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=; C_WARN=; C_FAIL=; C_DIM=; C_OFF=
fi
n_warn=0; failed=""
ok()   { printf '%s[ ok ]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
info() { printf '%s[info]%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_WARN" "$C_OFF" "$*"; n_warn=$((n_warn + 1)); }
fail() { # fail <short name> <message>
  printf '%s[FAIL]%s %s\n' "$C_FAIL" "$C_OFF" "$2"
  failed="${failed:+$failed, }$1"
}
have() { command -v "$1" >/dev/null 2>&1; }
# Last-pass sanitiser (python/jq already removed control, bidi and invisible characters).
clean() { printf '%s' "$1" | tr -d '\000-\037\177'; }
refuse() { echo "refused: $*; nothing changed."; exit 2; }
q() { printf '%q' "$1"; }   # shell-quote a path for copy-paste hints

# ---- 1. Context -------------------------------------------------------------------------
# Everything below acts on the repository of this worktree only: the main checkout is always
# derived from git; ORCA_ROOT_PATH is just a cross-check.
start="${ORCA_WORKTREE_PATH:-.}"
cd "$start" 2>/dev/null || refuse "cannot enter '$start'"
wt="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$wt" ] || ! cd "$wt"; then refuse "'$start' is not inside a git worktree"; fi
wt="$(pwd -P)"
common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
main="$(cd "$(dirname "$common")" 2>/dev/null && pwd -P)"
[ -n "$main" ] || refuse "cannot locate the main checkout of '$wt'"
branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)"
info "worktree ${wt} | branch ${branch:-(detached HEAD)} | main checkout ${main}"

# ---- 2. Guard: only this repo -----------------------------------------------------------
# Host must be exactly github.com (optional scheme and user@); userinfo is never printed.
origin="$(git remote get-url origin 2>/dev/null)"
case "$origin" in *"
"*) refuse "origin URL contains a newline" ;; esac
origin_shown="$(printf '%s' "$origin" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)?.*@#\1#')"
origin_lc="$(printf '%s' "$origin" | tr '[:upper:]' '[:lower:]')"
if ! printf '%s\n' "$origin_lc" | grep -Eq \
    "^((https?|ssh|git)://)?([^/@]+@)?github\.com(:[0-9]+)?[:/]+${REPO_SLUG}(\.git)?/?$"; then
  refuse "origin is '${origin_shown:-none}', not ${REPO_SLUG}"
fi
ok "origin is ${REPO_SLUG}"
if [ -n "${ORCA_ROOT_PATH:-}" ] && [ "$(cd "$ORCA_ROOT_PATH" 2>/dev/null && pwd -P)" != "$main" ]; then
  warn "ORCA_ROOT_PATH ($ORCA_ROOT_PATH) is not this worktree's main checkout; using $main"
fi

# Token availability decides whether this run may change anything. GH_TOKEN/GITHUB_TOKEN
# are dropped so the check is about the stored visionary-sender login, not an env override.
token_ok=0
gh_token() { env -u GH_TOKEN -u GITHUB_TOKEN gh auth token -u "$GH_USER" 2>/dev/null; }
if have gh && [ -n "$(gh_token)" ]; then token_ok=1; fi
mutate=$token_ok

# ---- 3. Git identity (repo-local only) --------------------------------------------------
local_email="$(git config --local --get user.email 2>/dev/null)"; id_note=""
if [ -z "$local_email" ]; then
  noreply=""
  if [ $mutate -eq 1 ]; then
    noreply="$(GH_TOKEN="$(gh_token)" gh api user \
      --jq "select(.login == \"$GH_USER\") | \"\(.id)+\(.login)@users.noreply.github.com\"" \
      2>/dev/null)"
  fi
  if [ -n "$noreply" ] && git config --local user.email "$noreply"; then
    local_email="$noreply"; id_note=" (repo-local user.email set now)"
  else
    fail "git identity" "git identity: no repo-local user.email and could not derive it; run:
       git -C $(q "$main") config --local user.email \"\$(GH_TOKEN=\$(gh auth token -u $GH_USER) gh api user --jq '\"\\(.id)+\\(.login)@users.noreply.github.com\"')\""
  fi
fi
if [ -n "$local_email" ]; then
  eff_name="$(git config --get user.name 2>/dev/null)"
  case "$local_email" in
    *"+$GH_USER@users.noreply.github.com")
      if [ -n "$eff_name" ]; then ok "git identity: $eff_name <$local_email>${id_note:-}"
      else warn "git identity: user.name is empty (email $local_email)"; fi ;;
    *) warn "git identity: repo-local user.email is $local_email, not the $GH_USER noreply address" ;;
  esac
fi

# ---- 4. Push credentials ----------------------------------------------------------------
if git config --local --get-all credential.helper 2>/dev/null | grep -q "gh auth token -u $GH_USER"; then
  helper_ok=1
else
  helper_ok=0
fi
if [ $helper_ok -eq 1 ] && [ $token_ok -eq 1 ]; then
  ok "push credentials: repo-local credential helper + gh token for $GH_USER"
else
  [ $helper_ok -eq 1 ] || fail "credential helper" "push credentials: no repo-local credential.helper using 'gh auth token -u $GH_USER' (see follow-the-fake-workflow references/setup.md, GitHub section)"
  [ $token_ok -eq 1 ] || fail "gh login" "push credentials: 'gh auth token -u $GH_USER' failed; log $GH_USER into gh again (gh auth login), then switch the active account back"
fi
[ $mutate -eq 1 ] || info "check-only mode (no $GH_USER token): this run changes nothing"

# ---- 5. Workflow hooks ------------------------------------------------------------------
installer="$SKILL_DIR/scripts/install-hooks.sh"
installed=""
if [ ! -f "$installer" ]; then
  fail "hooks" "hooks: workflow skill missing ($SKILL_DIR); commit-msg/pre-commit cannot be installed"
else
  [ $mutate -eq 1 ] && installed="$(sh "$installer" "$main" 2>&1 | grep '^installed' | sed 's/^installed //' | tr '\n' ' ')"
  if check_out="$(sh "$installer" --check "$main" 2>&1)"; then
    if [ -n "$installed" ]; then ok "hooks: installed ${installed% }; commit-msg + pre-commit verified"
    else ok "hooks: commit-msg + pre-commit linked"; fi
  else
    fail "hooks" "hooks: $(printf '%s\n' "$check_out" | sed -n 's/^MISSING \([^ ]*\).*/\1/p' | tr '\n' ' ')missing; run $(q "$installer") $(q "$main")"
  fi
fi

# ---- 6. Branch <-> Linear issue ---------------------------------------------------------
issue="$(printf '%s\n' "$branch" | tr '[:upper:]' '[:lower:]' \
  | sed -n -E 's#^(.*/)?(vis)-([0-9]+)(-.*)?$#\2-\3#p' | tr '[:lower:]' '[:upper:]')"
if [ -z "$branch" ]; then
  warn "branch: detached HEAD; one issue = one branch"
elif [ -z "$issue" ]; then
  if [ "$branch" = main ]; then info "branch: main (no issue)"
  else warn "branch: '$branch' has no vis-<n>; one issue = one branch"; fi
elif ! have orca; then
  warn "issue $issue: orca CLI not found, cannot look it up"
else
  # Portable timeout (macOS has no coreutils timeout). The lookup runs in its own process
  # group (set by child and parent, so an early alarm still finds it). On alarm, INT/TERM/HUP
  # or normal exit the group is TERMed (then KILLed on alarm), so no orphan stays behind.
  # Output goes to a private temp file, not a pipe, so a child holding stdout cannot block;
  # the traps remove the file on any exit, including Ctrl-C.
  out="$(mktemp "${TMPDIR:-/tmp}/orca-setup.XXXXXX")" || out=""
  json=""
  if [ -n "$out" ]; then
    trap 'rm -f "$out"' EXIT; trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP
    perl -e '$t = shift; $pid = fork; defined $pid or die "fork: $!\n";
      if (!$pid) { setpgrp(0, 0); exec @ARGV; exit 127 }
      setpgrp($pid, $pid);
      sub zap { my $s = shift; kill($s, -$pid) or kill($s, $pid) }
      $SIG{$_} = sub { zap("TERM"); exit 130 } for qw(INT TERM HUP);
      $SIG{ALRM} = sub { zap("TERM"); select(undef, undef, undef, 0.5);
                         zap("KILL") if kill(0, -$pid) || kill(0, $pid); exit 124 };
      alarm $t; waitpid($pid, 0); my $rc = $? >> 8; zap("TERM"); exit $rc' "$LINEAR_TIMEOUT" \
      orca linear issue "$issue" --workspace "$LINEAR_WS" --json >"$out" 2>/dev/null </dev/null
    json="$(cat "$out")"
    rm -f "$out"
  fi
  # Linear text is untrusted: control, bidi and invisible characters are removed and the
  # length is capped (in code points). Python (primary) drops every C* category and Zl/Zp;
  # the jq fallback drops the equivalent code-point ranges; clean() is a last pass.
  title=""; state=""; type_line=""; bname=""
  if [ -n "$json" ] && have python3; then
    parsed="$(printf '%s' "$json" | python3 -I -c 'import json, sys, unicodedata
def clean(s, n=None):
    s = "".join(c for c in (s or "") if unicodedata.category(c)[0] != "C"
                and unicodedata.category(c) not in ("Zl", "Zp"))
    return s[:n] if n else s
i = (json.load(sys.stdin).get("result") or {}).get("issue") or {}
print(clean(i.get("title"), 100))
print(clean((i.get("state") or {}).get("name"), 30))
print(clean((i.get("description") or "").split("\n")[0], 120))
print(clean(i.get("branchName")))' 2>/dev/null)"
    title="$(printf '%s\n' "$parsed" | sed -n 1p)"
    state="$(printf '%s\n' "$parsed" | sed -n 2p)"
    type_line="$(printf '%s\n' "$parsed" | sed -n 3p)"
    bname="$(printf '%s\n' "$parsed" | sed -n 4p)"
  elif [ -n "$json" ] && have jq; then
    # shellcheck disable=SC2016  # $n is a jq variable
    jq_clean='def clean: explode | map(select(
        . < 32 or (. >= 127 and . <= 159) or . == 173 or (. >= 1536 and . <= 1541)
        or . == 1564 or . == 1757 or . == 1807 or . == 2192 or . == 2193 or . == 2274
        or . == 6158 or (. >= 8203 and . <= 8207) or (. >= 8232 and . <= 8238)
        or (. >= 8288 and . <= 8303) or (. >= 55296 and . <= 57343)
        or (. >= 57344 and . <= 63743) or (. >= 64976 and . <= 65007) or . == 65279
        or (. >= 65529 and . <= 65531) or (. % 65536) >= 65534
        or . == 69821 or . == 69837 or (. >= 78896 and . <= 78911)
        or (. >= 113824 and . <= 113827) or (. >= 119155 and . <= 119162)
        or (. >= 262144 and . <= 917503) or (. >= 917504 and . <= 921599) or . >= 983040
      | not)) | implode;
      def cap($n): explode | .[0:$n] | implode;'
    jqf() { printf '%s' "$json" | jq -r "$jq_clean $1" 2>/dev/null; }
    title="$(jqf '.result.issue.title // "" | clean | cap(100)')"
    state="$(jqf '.result.issue.state.name // "" | clean | cap(30)')"
    type_line="$(jqf '(.result.issue.description // "") | split("\n")[0] | clean | cap(120)')"
    bname="$(jqf '.result.issue.branchName // "" | clean')"
  fi
  if [ -n "$title" ]; then
    ok "issue $issue: $(clean "$title") [$(clean "$state")]"
    case "$type_line" in
      Type:*) info "  $(clean "$type_line")" ;;
      *) warn "issue $issue: description has no 'Type:' first line" ;;
    esac
    # branchName is untrusted: print it only if it is a plain ref name starting with a letter
    # or digit (never an option like --force). Never rename here.
    if [ -n "$bname" ] && [ "$bname" != "$branch" ]; then
      if printf '%s\n' "$bname" | LC_ALL=C grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/-]*$'; then
        warn "branch differs from the issue's branchName; rename with: git branch -m '$bname'"
      else
        warn "branch differs from the issue's branchName (not shown: unexpected characters)"
      fi
    fi
  else
    warn "issue $issue: Linear lookup failed or timed out (${LINEAR_TIMEOUT}s); not fatal"
  fi
fi

# ---- 7. Cloud accounts ------------------------------------------------------------------
# A blocked .envrc means CLOUDSDK_ACTIVE_CONFIG_NAME is unset and gcloud falls back to the
# global default configuration (another project), so it fails. Checked for this worktree
# and the main checkout; changing the tracked .envrc re-blocks it until allowed again.
envrc_state() { # prints 0 allowed, 1 not allowed, 2 denied (direnv's codes), empty if unknown
  local js
  js="$(cd "$1" 2>/dev/null && direnv status --json 2>/dev/null)"
  if have jq; then
    printf '%s' "$js" | jq -r '.state.foundRC.allowed // empty' 2>/dev/null
  else
    printf '%s' "$js" | tr -d ' \t\n' | sed -n 's/.*"foundRC":{"allowed":\([0-9]\).*/\1/p'
  fi
}
envrc_dirs=""
[ -f "$wt/.envrc" ] && envrc_dirs="$wt"
[ "$main" != "$wt" ] && [ -f "$main/.envrc" ] && envrc_dirs="$envrc_dirs${envrc_dirs:+
}$main"
if [ -n "$envrc_dirs" ] && ! have direnv; then
  fail "direnv" "direnv: not installed; .envrc is not loaded and gcloud would use the default configuration"
elif [ -n "$envrc_dirs" ]; then
  allowed_now=""; blocked=""; where=""
  while IFS= read -r d; do
    if [ "$d" = "$wt" ]; then label="worktree"; else label="main checkout"; fi
    where="${where:+$where + }$label"
    st="$(envrc_state "$d")"
    # Only "not allowed" (1) is allowed here. Denied (2) was the user's choice and an
    # unknown state may be a denial: both are only reported.
    if [ "$st" = 1 ] && [ $mutate -eq 1 ] && direnv allow "$d" >/dev/null 2>&1; then
      st="$(envrc_state "$d")"; [ "$st" = 0 ] && allowed_now="${allowed_now:+$allowed_now, }$label"
    fi
    [ "$st" = 0 ] || blocked="$blocked${blocked:+ && }direnv allow $(q "$d")"
  done <<EOF
$envrc_dirs
EOF
  if [ -n "$blocked" ]; then
    fail "direnv" "direnv: .envrc blocked (gcloud would use the default configuration); run: $blocked"
  elif [ -n "$allowed_now" ]; then
    ok "direnv: .envrc allowed ($where); ran direnv allow for: $allowed_now"
  else
    ok "direnv: .envrc allowed ($where)"
  fi
fi
gconf="${CLOUDSDK_CONFIG:-$HOME/.config/gcloud}/configurations/config_$GCLOUD_CONF"
if [ ! -f "$gconf" ]; then
  fail "gcloud config" "gcloud: configuration '$GCLOUD_CONF' missing (gcloud config configurations create $GCLOUD_CONF)"
else
  gproj="$(awk -F'=' '/^\[/{s=$0} s=="[core]" && $1 ~ /^[ \t]*project[ \t]*$/ {gsub(/[ \t]/,"",$2); print $2; exit}' "$gconf")"
  if [ "$gproj" = "$GCLOUD_PROJECT" ]; then
    ok "gcloud: configuration '$GCLOUD_CONF' -> $GCLOUD_PROJECT"
  else
    fail "gcloud project" "gcloud: configuration '$GCLOUD_CONF' points at '${gproj:-nothing}', expected $GCLOUD_PROJECT"
  fi
fi

# ---- 8. Linear API key (existence only) -------------------------------------------------
if ! have security; then
  warn "linear key: no macOS 'security' tool, cannot check the keychain"
elif security find-generic-password -s linear-api-giraffes -a "$GH_USER" >/dev/null 2>&1; then
  ok "linear key: present in keychain (linear-api-giraffes)"
else
  warn "linear key: missing in keychain (service linear-api-giraffes, account $GH_USER); linear_api.py will not work"
fi

# ---- 8b. Local agent files (gitignored, linked from the main checkout) -----------------
if [ "$wt" != "$main" ]; then
  for f in CLAUDE.md .claude/settings.json; do
    if [ -e "$wt/$f" ] || [ -L "$wt/$f" ]; then
      ok "agent file: $f present"
    elif [ ! -f "$main/$f" ]; then
      warn "agent file: $f missing in main checkout; nothing to link"
    elif [ $mutate -eq 1 ] && mkdir -p "$(dirname "$wt/$f")" && ln -s "$main/$f" "$wt/$f"; then
      ok "agent file: linked $f from main checkout"
    else
      warn "agent file: $f not linked (run: ln -s $(q "$main/$f") $(q "$wt/$f"))"
    fi
  done
fi

# ---- 9. Data guard ---------------------------------------------------------------------
tracked="$(git ls-files -- BACKUP 'do_udostępnienia' data '*.pdf' '*.har' 2>/dev/null | head -3)"
if [ -n "$tracked" ]; then
  fail "data in git" "data tracked in git (e.g. $(printf '%s' "$tracked" | head -1)); data lives only in gs://follow-the-fake-data"
else
  ok "no challenge data tracked in git"
fi

# ---- 10. Rules --------------------------------------------------------------------------
info "rule: data stays in gs://follow-the-fake-data, never in git"
info "rule: paid GCP work needs a cost estimate and an automatic stop"
info "rule: merge only on the owner's explicit OK"

# ---- Summary ----------------------------------------------------------------------------
if [ -n "$failed" ]; then
  printf '%sNOT READY%s: %s%s\n' "$C_FAIL" "$C_OFF" "$failed" \
    "$([ $mutate -eq 1 ] || echo ' (check-only, nothing changed)')"
  exit 1
elif [ $n_warn -gt 0 ]; then
  printf '%sready%s with %d warning(s)\n' "$C_WARN" "$C_OFF" "$n_warn"
else
  printf '%sready%s\n' "$C_OK" "$C_OFF"
fi
exit 0

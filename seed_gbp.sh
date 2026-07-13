#!/usr/bin/env bash
# seed_gbp.sh — seed ONLY the GBP campaign files onto Render's /data disk.
#
# Unlike seed_data.sh, this touches nothing else — it will NOT overwrite
# leads.csv, sms_history.csv, pipeline_state.json, etc. Use this to get the
# campaign live on the hosted dashboard.
#
# How to use:
#   1. Run locally in the repo dir:  bash seed_gbp.sh
#   2. Copy the printed one-liner.
#   3. Render dashboard -> your service -> Shell tab -> paste it.
#
# Seeds:
#   gbp_token.json     - OAuth token (auth; self-refreshes after this)
#   gbp_campaign.json  - campaign progress so the server continues from where
#                        we are now (never re-posts what already went out)

set -euo pipefail
cd "$(dirname "$0")"

FILES=(gbp_token.json gbp_campaign.json)

MISSING=()
for f in "${FILES[@]}"; do
    [[ -f "$f" ]] || MISSING+=("$f")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "Missing locally: ${MISSING[*]}" >&2
    exit 1
fi

echo "Will seed ${#FILES[@]} file(s) onto /data:"
for f in "${FILES[@]}"; do
    printf "  %-22s %s bytes\n" "$f" "$(wc -c < "$f")"
done

TARBALL=$(tar czf - "${FILES[@]}" | base64)

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COPY THE COMMAND BELOW AND PASTE IT INTO RENDER'S SHELL TAB.
It writes ONLY the two GBP files into /data — nothing else is touched.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

cd /data && echo '${TARBALL}' | base64 -d | tar xzf - && ls -la gbp_token.json gbp_campaign.json

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF

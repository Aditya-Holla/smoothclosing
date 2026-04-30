#!/usr/bin/env bash
# seed_data.sh — print a copy-paste script to seed Render's /data disk.
#
# How to use:
#   1. Run this from your laptop in the repo dir: `bash seed_data.sh`
#   2. It prints a base64-encoded tarball of your local data files plus a
#      one-liner that decodes + extracts them into /data on Render.
#   3. Open Render dashboard → your service → Shell tab.
#   4. Paste the one-liner. Done.
#
# Why this approach: Render's web Shell doesn't have scp/rsync, but it can
# run any command. Encoding the files inline avoids needing extra tooling.

set -euo pipefail

cd "$(dirname "$0")"

FILES=(
    leads.csv
    leads_new.csv
    leads_new_equity.csv
    leads_new_traced.csv
    leads_new_sms_sent.csv
    leads_with_equity.csv
    sms_history.csv
    skip_genie_results.csv
    pipeline_state.json
)

# Build list of files that actually exist locally.
EXISTING=()
for f in "${FILES[@]}"; do
    [[ -f "$f" ]] && EXISTING+=("$f")
done

if [[ ${#EXISTING[@]} -eq 0 ]]; then
    echo "No data files found in $(pwd) — nothing to seed." >&2
    exit 1
fi

echo "Will seed ${#EXISTING[@]} file(s):"
for f in "${EXISTING[@]}"; do
    size=$(wc -c < "$f")
    printf "  %-32s %s bytes\n" "$f" "$size"
done

# Include input_pdfs/ if it exists and has any PDFs.
INCLUDE_PDFS=""
if [[ -d input_pdfs ]] && compgen -G "input_pdfs/*.pdf" > /dev/null; then
    pdf_count=$(find input_pdfs -name "*.pdf" -maxdepth 1 | wc -l | tr -d ' ')
    INCLUDE_PDFS="input_pdfs"
    echo "  input_pdfs/                      ($pdf_count PDFs)"
fi

# Build the tarball (gzipped, base64'd) so it's a single string.
TARBALL=$(tar czf - "${EXISTING[@]}" ${INCLUDE_PDFS} | base64)

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COPY THE COMMAND BELOW AND PASTE IT INTO RENDER'S SHELL TAB.
It will recreate every file shown above inside /data on the server.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

cd /data && echo '${TARBALL}' | base64 -d | tar xzf - && ls -la

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF

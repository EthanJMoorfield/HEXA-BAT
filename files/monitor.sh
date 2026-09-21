#!/bin/bash

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROF_DIR="${PROF_DIR:-$BASE_DIR/profiling}"
EXEC_DIR="$PROF_DIR/exec"

mkdir -p "$EXEC_DIR"

HEADAS_BIN="$(dirname "$(which batsurvey)")"

for real_path in "$HEADAS_BIN"/*; do
    tool=$(basename "$real_path")

    [ -x "$real_path" ] || continue
    [ -f "$real_path" ] || continue

    cat > "$EXEC_DIR/$tool" <<EOF

#!/bin/bash
real="$HEADAS_BIN/$tool"
start=\$(date +%s.%N)
"\$real" "\$@"
status=\$?
end=\$(date +%s.%N)
echo "\$(basename "\$0"),\$\$,\$start,\$end,\$(echo "\$end - \$start" | bc)" >> "$PROF_DIR/timing.txt"
exit \$status
EOF

    chmod +x "$EXEC_DIR/$tool"
done

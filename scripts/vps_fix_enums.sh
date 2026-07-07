#!/bin/bash
# Run ENUM fixes — ALTER TYPE ADD VALUE cannot run inside a transaction.
# Manually parses DATABASE_URL to avoid # breaking URL parsers.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env not found at $ENV_FILE"
    exit 1
fi

RAW_URL=$(grep '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)

# Parse via python using regex on the raw (still %-encoded) URL
# so # never confuses the parser
read DB_HOST DB_PORT DB_USER DB_NAME < <(python3 -c "
import re, urllib.parse, sys
url = sys.argv[1]
# regex on raw URL before any decoding
m = re.match(r'postgresql://([^:]+):([^@]+)@([^:/]+):?(\d+)?/([^?#]+)', url)
if not m:
    print('PARSE_ERROR', '5432', 'PARSE_ERROR', 'PARSE_ERROR')
    sys.exit(1)
user  = urllib.parse.unquote(m.group(1))
# password not needed here
host  = m.group(3)
port  = m.group(4) or '5432'
db    = m.group(5)
print(host, port, user, db)
" "$RAW_URL")

echo "Host=$DB_HOST  Port=$DB_PORT  User=$DB_USER  DB=$DB_NAME"
echo ""
read -s -p "Enter password for $DB_USER: " PGPASSWORD
echo ""
export PGPASSWORD

run_psql() {
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1"
}

echo "Adding PAY_LATER to paymentmethod enum..."
run_psql "ALTER TYPE paymentmethod ADD VALUE IF NOT EXISTS 'PAY_LATER';"

echo "Adding CANCELLED to paymentstatus enum..."
run_psql "ALTER TYPE paymentstatus ADD VALUE IF NOT EXISTS 'CANCELLED';"

echo ""
echo "Verifying enum values..."
run_psql "
SELECT t.typname AS enum_name, e.enumlabel AS value
FROM pg_enum e
JOIN pg_type t ON t.oid = e.enumtypid
WHERE t.typname IN ('paymentmethod','paymentstatus')
ORDER BY t.typname, e.enumsortorder;
"

echo "Done."
unset PGPASSWORD

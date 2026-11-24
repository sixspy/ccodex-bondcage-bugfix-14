import sqlite3
import subprocess
import sys

DB = "/workspace/data/app.db"
ca = sqlite3.connect(DB)
ca.execute("PRAGMA journal_mode=DELETE")
ca.execute("UPDATE bond_accounts SET frozen=777 WHERE party_key='P-NORTH'")
ca.commit()
ca.close()
print("writer committed frozen=777")

c1 = sqlite3.connect(DB)
print("same-process read:", c1.execute(
    "SELECT frozen FROM bond_accounts WHERE party_key='P-NORTH'").fetchone()[0])
c1.close()

code = (
    "import sqlite3;c=sqlite3.connect('%s');"
    "print(c.execute(\"SELECT frozen FROM bond_accounts WHERE party_key='P-NORTH'\").fetchone()[0])"
    % DB
)
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
print("new-process read:", out.stdout.strip(), out.stderr.strip()[:100])

c2 = sqlite3.connect(DB)
c2.execute("UPDATE bond_accounts SET frozen=80000 WHERE party_key='P-NORTH'")
c2.commit()
c2.close()
print("restored to 80000")

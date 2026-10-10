from datetime import datetime,timezone
from pathlib import Path
import subprocess
root=Path(__file__).resolve().parents[1]
destination=root/'backups';destination.mkdir(exist_ok=True)
file=destination/('sportedge_v6_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.dump')
with file.open('wb') as handle:
    process=subprocess.run(['docker','compose','-f',str(root/'compose.yaml'),'exec','-T','v6-db','pg_dump','-U','v6','-d','sportedge_v6','-Fc'],stdout=handle,stderr=subprocess.PIPE)
if process.returncode:raise SystemExit('V6 backup failed')
print('V6 backup saved: '+str(file))

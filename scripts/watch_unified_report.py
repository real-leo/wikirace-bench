"""Finish the benchmark's local summary/report after each completed episode.

This process only reads benchmark outputs and writes the two named report files.
It exits when the controller finishes or disappears; it never launches new races.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--controller-pid',type=int,required=True)
    args=parser.parse_args()
    summary=ROOT/'reports/2026-09-21-unified-summary.json'
    report=ROOT/'reports/2026-09-21-unified-comparison.md'
    previous=None
    while True:
        try:
            manifest=json.loads((args.root/'manifest.json').read_text())
        except json.JSONDecodeError:
            time.sleep(2)
            continue
        try:
            os.kill(args.controller_pid,0)
            alive=True
        except ProcessLookupError:
            alive=False
        files=sorted(args.root.glob('race/*/hard-*.summary.json'))
        signature=(len(files),manifest.get('complete',False),alive)
        if signature != previous:
            subprocess.run([sys.executable,str(ROOT/'scripts/summarize_unified_comparison.py'),
                            '--root',str(args.root),'--out',str(summary)],check=True,cwd=ROOT)
            data=json.loads(summary.read_text())
            data['controller_alive']=alive
            data['execution_status']='complete' if manifest.get('complete') else ('running' if alive else 'stopped')
            temporary=summary.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
            temporary.replace(summary)
            subprocess.run([sys.executable,str(ROOT/'scripts/render_unified_report.py'),
                            '--summary',str(summary),'--out',str(report)],check=True,cwd=ROOT)
            print('REPORT',json.dumps({'episodes':len(files),'complete':manifest.get('complete',False),'controller_alive':alive}),flush=True)
            previous=signature
        if manifest.get('complete') or not alive:
            return
        time.sleep(30)


if __name__=='__main__':
    main()

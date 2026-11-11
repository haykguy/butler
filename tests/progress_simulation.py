#!/usr/bin/env python3
"""Read-only, synthetic-scenario simulation; calls Anthropic CLI, never sends messages."""
import os
import ast, datetime, json, pathlib, subprocess, sys, time
ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / 'progress_simulation_results.json'
source = subprocess.check_output(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=5',os.environ.get('BUTLER_SIM_HOST', 'remote-host'),'cat ~/.hermes/plugins/imessage-plus/__init__.py'], text=True)
tree = ast.parse(source)
judge = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'INTERIM_SYSTEM' for t in n.targets))
scenarios = [
 {'id':'quiet_narration','context':'User: Compare three laptop models for me; let me know when you have a recommendation.\nEvidence so far: One manufacturer page opened successfully; two models remain unchecked. No user input is needed. No progress update was requested. Task is ongoing.', 'expected':'SKIP'},
 {'id':'real_blocker','context':'User: Please book my appointment for Friday. Ask me if you need a choice.\nEvidence so far: The booking page offers Friday at 10:00 AM or 2:00 PM. Neither time has been selected, and the site requires selecting one to continue. No booking has been made.', 'expected':'SEND'},
]
def call(system, user):
 start=datetime.datetime.now(datetime.timezone.utc).isoformat()
 cmd=['claude','-p','--model','haiku','--tools','','--no-session-persistence','--output-format','json','--system-prompt',system,user]
 p=subprocess.run(cmd, text=True, capture_output=True, timeout=120, cwd=str(ROOT))
 end=datetime.datetime.now(datetime.timezone.utc).isoformat()
 if p.returncode: raise RuntimeError('CLI failed: '+str(p.returncode)+' '+p.stderr[-1000:])
 data=json.loads(p.stdout)
 if data.get('is_error'): raise RuntimeError(str(data.get('result')))
 return {'start_utc':start,'end_utc':end,'system':system,'user':user,'result':data['result'],'modelUsage':data.get('modelUsage'),'duration_ms':data.get('duration_ms')}
results={'description':'Two predeclared illustrative scenarios. Actual production judge sees only candidate, not original context, matching imessage-plus implementation. Single assessor sees candidate and original evidence. No message delivery.', 'judge_source':'<remote-host> ~/.hermes/plugins/imessage-plus/__init__.py INTERIM_SYSTEM, read-only', 'cases':[]}
for case in scenarios:
 candidate=call('You are an assistant working on a task. Draft one natural, brief mid-task update using only the supplied evidence; do not choose silence. Output only the update.',case['context'])
 draft=candidate['result']
 single=call('You are the working assistant deciding whether to send a mid-task update. Send only if a real user-relevant milestone, unexpected delay, blocker, needed input or change of plan warrants it. Otherwise output exactly SKIP. If sending, output only a concise casual text. Use only the supplied evidence; do not invent facts.',case['context']+'\nCandidate update: '+draft)
 judged=call(judge,draft[:4000])
 results['cases'].append({'scenario':case,'candidate':candidate,'single':single,'production_judge':judged})
 OUT.write_text(json.dumps(results,indent=2)+'\n')
 print(case['id'], 'candidate=',repr(draft),'single=',repr(single['result']),'judge=',repr(judged['result']),flush=True)
print('saved',OUT)

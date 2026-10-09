#!/usr/bin/env python3
import json,os,pathlib,subprocess
root=pathlib.Path(__file__).resolve().parents[2]
state=root/'.local/repair-rke2'
credentials=json.loads((state/'dependencies.json').read_text())
env=os.environ.copy();env.update({'ANVILKIT_REPAIR_KUBERNETES':'1','KUBECONFIG':str(state/'kubeconfig.yaml'),'ANVILKIT_INTEGRATION_API_URL':'http://127.0.0.1:29100','ANVILKIT_INTEGRATION_TOKEN_A':credentials['api_token_a'],'ANVILKIT_INTEGRATION_TOKEN_B':credentials['api_token_b'],'ANVILKIT_DEV_CONTROL_DSN':f"postgres://anvilkit_control_app:{credentials['database_password']}@127.0.0.1:25434/anvilkit_control?sslmode=disable"})
raise SystemExit(subprocess.call(['go','test','-C',str(root/'tests/integration'),'-tags','integration','-run','TestDeployedRepairChain','-count=1','-v','./...'],env=env))

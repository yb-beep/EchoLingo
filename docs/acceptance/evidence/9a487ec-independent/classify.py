import re,json,collections
from pathlib import Path
p=Path('/tmp/cet-9a487ec-audit');s=(p/'contract-real-controlled.txt').read_text();names=re.findall(r'^FAILED tests/test_cet_contract.py::(\w+)',s,re.M)
groups={
'响应包装、字段或枚举':[4,5,8,9,14,26,40],
'复习事件/actionId/响应前置':[10,11,18,19,23,24,25,37,38],
'播放事件协议及计时字段':[7,15,20,29,30,31],
'步骤/交卷/错误包装前置':[16,17,21,22,32,36],
'AI 查询取消测试前置':[33,34,35],
'句子 fixture 身份或边界策略':[2,27,28],
'本地动作 actionId':[6,39],
'作答冲突或候选可用性':[1,13],
'慢响应注入方法':[12],
'非法句子错误码约定':[3]}
reverse={i:k for k,values in groups.items() for i in values};assert len(names)==len(reverse)==40
rows=[{'index':i,'test':n,'firstFailureGroup':reverse[i]} for i,n in enumerate(names,1)]
(p/'contract-failures.json').write_text(json.dumps({'counts':{k:len(v) for k,v in groups.items()},'failures':rows},ensure_ascii=False,indent=2))
print(json.dumps({k:len(v) for k,v in groups.items()},ensure_ascii=False))

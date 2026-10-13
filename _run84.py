import os, paramiko
c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.101.84", username="zhu", password=os.environ["SSHPASS_84"], timeout=15, look_for_keys=False, allow_agent=False)
def run(cmd, t=900):
    _i, o, e = c.exec_command(cmd, timeout=t)
    return o.channel.recv_exit_status(), o.read().decode("utf-8","replace").strip(), e.read().decode("utf-8","replace").strip()

# 选 3 个之前 retry_wait 的国网源 + 1 个已知成功的对照
SOURCES = ["sgcc_tj", "sgcc_he", "sgcc_zj", "sgcc_sc"]
print("在 84 容器内逐个跑（每个单独计时）\n")
for src in SOURCES:
    print(f"--- {src} ---")
    cmd = (f"docker exec -e PYTHONPATH=/app power-worker "
           f"python -m newsagg crawl-once --source {src} --days 30 --exec 2>&1 "
           f"| grep -E '备选|阻断|完成:|执行完成|ERROR' | tail -4")
    code, o, e = run(cmd, t=600)
    for line in (o or e).splitlines(): print(f"   {line[:170]}")
    print()
c.close()

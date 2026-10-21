"""================================================================
XTMEM · 公开安装验收导游图

▍这是什么
用合成数据验证真正安装后的CLI，不借生产库、宿主目录或真实会话。
Python标准库与pytest；CI和发布者均可运行。

▍修改铁律
子进程必须显式指定临时目录和身份，禁止继承宿主、管理员及生产库环境。

▍更新记录
2026-10-22：覆盖完整命令入口、跨角色可见性、归档与重试、无Ollama降级。
================================================================"""
import json
import os
from pathlib import Path
import subprocess
import sys


def run_cli(root, actor, *args, expected=0):
    """隔离全部运行状态；断网向量地址用于验证关键词降级，不替换引擎。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(('XTMEM_', 'HERMES_', 'MIMOX_'))}
    env.pop('PYTHONPATH', None)
    env.update(XTMEM_HOME=str(root), XTMEM_DB_PATH=str(root / 'memory.db'),
               XTMEM_ACTOR=actor, XTMEM_EMBEDDING_URL='http://127.0.0.1:1',
               XTMEM_EMBEDDING_TIMEOUT='1', PYTHONUTF8='1')
    p = subprocess.run([sys.executable, '-m', 'xtmem', *args], cwd=root,
                       env=env, capture_output=True, text=True, encoding='utf-8', timeout=25)
    data = json.loads(p.stdout)
    assert p.returncode == expected, (p.returncode, data, p.stderr[-500:])
    return data


def test_installed_commands_and_output(tmp_path):
    p = subprocess.run([sys.executable, '-m', 'xtmem', '--help'], cwd=tmp_path,
                       capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert p.returncode == 0
    for command in ['store', 'recall', 'forget', 'reembed', 'serve', 'doctor', 'msg', 'trace']:
        assert command in p.stdout
    assert run_cli(tmp_path, 'role-a', 'agents')['ok'] is True


def test_shared_private_and_archive(tmp_path):
    shared = run_cli(tmp_path, 'role-a', 'store', '--agent', 'role-a', '--namespace', 'role-a',
                     '--text', 'PUBLICQUARTZ712 database decision is SQLite', '--visibility', 'shared')
    private = run_cli(tmp_path, 'role-a', 'store', '--agent', 'role-a', '--namespace', 'role-a',
                      '--text', 'PRIVATEQUARTZ918 deployment note', '--visibility', 'private')
    assert shared['ok'] and private['ok']
    public_result = run_cli(tmp_path, 'role-b', 'lookup', '--query', 'PUBLICQUARTZ712', '--cross')
    assert shared['id'] in [x['id'] for x in public_result['items']]
    hidden = run_cli(tmp_path, 'role-b', 'lookup', '--query', 'PRIVATEQUARTZ918', '--cross')
    assert not hidden['items']
    recalled = run_cli(tmp_path, 'role-b', 'recall', '--query', 'PUBLICQUARTZ712', '--cross')
    assert shared['id'] in [x['id'] for x in recalled['items']]
    refused = run_cli(tmp_path, 'role-b', 'store', '--agent', 'role-b', '--namespace', 'role-a',
                       '--text', 'forbidden write', expected=1)
    assert refused['ok'] is False
    preview = run_cli(tmp_path, 'role-a', 'forget', '--id', private['id'])
    assert preview['pending'] == 1 and preview['removed'] == 0
    assert run_cli(tmp_path, 'role-a', 'forget', '--id', private['id'], '--confirm')['removed'] == 1
    after = run_cli(tmp_path, 'role-a', 'recall', '--query', 'PRIVATEQUARTZ918', '--cross')
    assert private['id'] not in [x['id'] for x in after['items']]


def test_installed_console_success_code(tmp_path):
    executable = Path(sys.executable).parent / ('xtmem.exe' if os.name == 'nt' else 'xtmem')
    env = dict(os.environ, XTMEM_HOME=str(tmp_path), XTMEM_DB_PATH=str(tmp_path / 'console.db'))
    for key in ['HERMES_HOME', 'MIMOX_HOME', 'XTMEM_HERMES_HOME', 'XTMEM_ADMIN', 'XTMEM_ADMIN_TOKEN']:
        env.pop(key, None)
    p = subprocess.run([str(executable), 'agents'], cwd=tmp_path, env=env,
                       capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)['ok'] is True

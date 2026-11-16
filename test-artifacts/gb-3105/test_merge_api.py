#!/usr/bin/env python3
"""工艺归并（process merge）端到端验证脚本。

覆盖需求点：
  1. 预览参数差异及样本影响（只读，无副作用）
  2. 确认归并：旧编号成为别名，新收样统一归入目标
  3. 历史样本分批迁移，迁完且数量核对一致才结束
  4. 旧评议保留原依据（快照与结果不随归并改变）
  5. 源与目标同时入选按一个工艺分析；归并后同组则拒绝
  6. 反向归并争抢：并发 A→B 与 B→A 只有一个成功
  7. 归并链落到最终工艺；循环归并拒绝
  8. 迁移半途（kill -9 worker）后从未完成处恢复，重试不重复移动样本
  9. 并发收样/修订参数/创建评议不丢样、不混用归并前后分组
 10. 数量核对不一致 → failed；重试后完成且不重复移动
 11. 权限：viewer 只读

运行前提：backend 在 127.0.0.1:19204，worker 由本脚本按需启停（stack.sh）。
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg

API = os.environ.get('API', 'http://127.0.0.1:19204')
DB = os.environ.get('DATABASE_URL', 'postgresql://seal@127.0.0.1:17704/seal')
STACK = '/workspace/.toolchain/stack.sh'

checks = []
def check(name, cond, detail=''):
    checks.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f'  [{detail}]' if detail and not cond else ''))

def worker(action):
    subprocess.run(['bash', STACK, {'start': 'start-worker', 'stop': 'stop-worker'}[action]],
                   check=False, capture_output=True, timeout=30)

def api(token, method, path, **kw):
    r = httpx.request(method, API + path, headers={'Authorization': f'Bearer {token}'}, timeout=30, **kw)
    return r

def login(email):
    r = httpx.post(API + '/auth/login', json={'email': email, 'password': 'Demo123!'}, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']

def wait_merge(token, mid, want, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        m = api(token, 'GET', f'/merges/{mid}').json()
        if m['status'] == want:
            return m
        time.sleep(0.5)
    raise TimeoutError(f'merge {mid} not {want}: {m}')

def wait_assessment(token, aid, want='completed', timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        a = api(token, 'GET', f'/assessments/{aid}').json()
        if a['status'] == want:
            return a
        if a['status'] == 'failed':
            raise AssertionError(f"assessment {aid} failed: {a['error']}")
        time.sleep(0.5)
    raise TimeoutError(f'assessment {aid} not {want}')

def mkprocess(token, code, name, **params):
    p = {'code': code, 'name': name, 'temperature': 160.0, 'dwell': 0.8, 'pressure': 0.32, 'note': '归并验证'}
    p.update(params)
    r = api(token, 'POST', '/processes', json=p)
    assert r.status_code == 201, r.text
    return r.json()

def mksamples(token, pid, prefix, n, lot='VM-01', force=23.0):
    for i in range(n):
        r = api(token, 'POST', '/samples', json={'code': f'{prefix}-{i+1:03}', 'process_id': pid,
                                                 'lot': lot, 'force': round(force + (i % 7) * 0.13, 2)})
        assert r.status_code == 201, r.text

def samples_of(token, pid):
    return [s for s in api(token, 'GET', '/samples').json() if s['process_id'] == pid]

def total_samples(token):
    return len(api(token, 'GET', '/samples').json())

def main():
    worker('stop')
    admin, analyst, viewer = login('admin@demo.local'), login('analyst@demo.local'), login('viewer@demo.local')
    procs = {p['code']: p for p in api(analyst, 'GET', '/processes').json()}
    s1, s2, s3 = procs['HC-S1'], procs['HC-S2'], procs['HC-S3']
    base_total = total_samples(analyst)

    # ---------- S1 预览：参数差异与样本影响，只读 ----------
    dup = mkprocess(analyst, 'VM-DUP', '稳温基线重复建档', temperature=165.0)
    mksamples(analyst, dup['id'], 'VMD', 5)
    prev = api(analyst, 'POST', '/merges/preview', json={'source_id': dup['id'], 'target_id': s1['id']}).json()
    check('S1.1 预览返回参数差异（温度不一致标记）',
          prev['ok'] and [d['changed'] for d in prev['diffs']] == [True, False, False])
    check('S1.2 预览样本影响（源5/目标35/合计40）',
          prev['source']['samples'] == 5 and prev['target']['samples'] == 35 and prev['after']['total_samples'] == 40)
    check('S1.3 预览无副作用（无归并记录、样本未动）',
          api(analyst, 'GET', '/merges').json() == [] and len(samples_of(analyst, dup['id'])) == 5)

    # ---------- S2 归并：别名、重定向、分批迁移完成 ----------
    m = api(analyst, 'POST', '/merges', json={'source_id': dup['id'], 'target_id': s1['id']}).json()
    check('S2.1 归并创建即进入迁移中', m['status'] == 'migrating' and m['expected_total'] == 5)
    alias = [p for p in api(analyst, 'GET', '/processes').json() if p['id'] == dup['id']][0]
    check('S2.2 旧编号立即成为别名', alias['merged_into_id'] == s1['id'])
    r = api(analyst, 'POST', '/samples', json={'code': 'VMD-NEW-1', 'process_id': dup['id'], 'lot': 'VM-01', 'force': 23.3})
    check('S2.3 归并期间新收样自动归入目标', r.status_code == 201 and r.json()['process_id'] == s1['id'])
    worker('start')
    m = wait_merge(analyst, m['id'], 'completed')
    worker('stop')
    check('S2.4 迁完且数量核对一致才结束', m['moved'] == 5 and m['remaining'] == 0 and m['status'] == 'completed')
    check('S2.5 源编号样本清零且总量不丢样',
          len(samples_of(analyst, dup['id'])) == 0 and total_samples(analyst) == base_total + 6)
    r = api(analyst, 'PUT', f"/processes/{dup['id']}",
            json={'code': 'VM-DUP', 'name': '改名', 'temperature': 161, 'dwell': 0.8, 'pressure': 0.32, 'note': ''})
    check('S2.6 别名编号冻结不可修订', r.status_code == 409)

    # ---------- S3 旧评议保留原依据 ----------
    dupb = mkprocess(analyst, 'VM-DUPB', '延时封合重复建档')
    mksamples(analyst, dupb['id'], 'VMB', 4)
    r = api(analyst, 'POST', '/assessments', json={'title': 'VM 归并前评议', 'process_ids': [dupb['id'], s2['id']], 'lot': '全部'})
    aid = r.json()['id']
    worker('start'); a_before = wait_assessment(analyst, aid); worker('stop')
    snap_before = json.dumps(a_before['snapshot'], sort_keys=True)
    m2 = api(analyst, 'POST', '/merges', json={'source_id': dupb['id'], 'target_id': s1['id']}).json()
    worker('start'); wait_merge(analyst, m2['id'], 'completed'); worker('stop')
    a_after = api(analyst, 'GET', f'/assessments/{aid}').json()
    check('S3.1 旧评议快照保留原编号与原分组', json.dumps(a_after['snapshot'], sort_keys=True) == snap_before
          and any(g['code'] == 'VM-DUPB' for g in a_after['snapshot']['groups']))
    check('S3.2 旧评议结果不变', a_after['result']['checksum'] == a_before['result']['checksum'])

    # ---------- S4 源与目标同时入选按一个工艺分析 ----------
    r = api(analyst, 'POST', '/assessments', json={'title': 'VM 别名同选去重', 'process_ids': [dup['id'], s1['id'], s2['id']], 'lot': '全部'})
    check('S4.1 别名与目标同选 → 两组', r.status_code == 202 and len(r.json()['snapshot']['groups']) == 2)
    g1 = [g for g in r.json()['snapshot']['groups'] if g['code'] == 'HC-S1'][0]
    check('S4.2 别名的样本并入目标组（35+5迁+1新+4迁=45）',
          g1['aliases'] == ['VM-DUP', 'VM-DUPB'] and len(g1['samples']) == 45)
    r = api(analyst, 'POST', '/assessments', json={'title': 'VM 同组拒绝', 'process_ids': [dup['id'], s1['id']], 'lot': '全部'})
    check('S4.3 归并后为同一工艺 → 422', r.status_code == 422)

    # ---------- S5 反向归并争抢 ----------
    ra = mkprocess(analyst, 'VM-RA', '反向A'); rb = mkprocess(analyst, 'VM-RB', '反向B')
    mksamples(analyst, ra['id'], 'VRA', 3); mksamples(analyst, rb['id'], 'VRB', 3)
    with ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(api, analyst, 'POST', '/merges', json={'source_id': ra['id'], 'target_id': rb['id']})
        f2 = ex.submit(api, analyst, 'POST', '/merges', json={'source_id': rb['id'], 'target_id': ra['id']})
        codes = sorted([f1.result().status_code, f2.result().status_code])
    check('S5.1 并发反向归并恰好一个成功', codes == [201, 409])
    winner = f1.result().json() if f1.result().status_code == 201 else f2.result().json()
    worker('start'); wait_merge(analyst, winner['id'], 'completed'); worker('stop')
    r = api(analyst, 'POST', '/merges', json={'source_id': winner['target_id'], 'target_id': winner['source_id']})
    check('S5.2 完成后反向归并仍按循环拒绝', r.status_code == 409)

    # ---------- S6 归并链落到最终工艺；循环拒绝 ----------
    loser_id = winner['target_id']  # 胜出的目标（现为正主）
    r = api(analyst, 'POST', '/merges', json={'source_id': loser_id, 'target_id': s3['id']})
    check('S6.1 链式归并 B→C 允许', r.status_code == 201)
    chain2 = r.json()
    r = api(analyst, 'POST', '/samples', json={'code': 'VRA-CHAIN', 'process_id': winner['source_id'], 'lot': 'VM-01', 'force': 24.0})
    check('S6.2 归并链落到最终工艺（A 的收样直入 C）', r.status_code == 201 and r.json()['process_id'] == s3['id'])
    worker('start'); wait_merge(analyst, chain2['id'], 'completed'); worker('stop')
    r = api(analyst, 'POST', '/merges', json={'source_id': s3['id'], 'target_id': winner['source_id']})
    check('S6.3 循环归并 C→A 拒绝', r.status_code == 409)
    r = api(analyst, 'POST', '/assessments', json={'title': 'VM 链同组拒绝', 'process_ids': [winner['source_id'], loser_id, s3['id']], 'lot': '全部'})
    check('S6.4 链上三者同选 → 422', r.status_code == 422)

    # ---------- S7 迁移半途恢复 ----------
    big = mkprocess(analyst, 'VM-BIG', '大批量旧档')
    mksamples(analyst, big['id'], 'VBG', 120)
    m7 = api(analyst, 'POST', '/merges', json={'source_id': big['id'], 'target_id': s2['id']}).json()
    worker('start')
    t0 = time.time(); mid_state = None
    while time.time() - t0 < 30:
        cur = api(analyst, 'GET', f"/merges/{m7['id']}").json()
        if 0 < cur['moved'] < 120:
            mid_state = cur; break
        time.sleep(0.2)
    worker('stop')  # kill -9，模拟中断
    check('S7.1 迁移半途中断（部分已迁）', mid_state is not None and 0 < mid_state['moved'] < 120,
          f'mid={mid_state}')
    time.sleep(1)
    stuck = api(analyst, 'GET', f"/merges/{m7['id']}").json()
    check('S7.2 中断后保持迁移中且进度未丢', stuck['status'] == 'migrating' and stuck['moved'] >= mid_state['moved'])
    before_codes = {s['code'] for s in api(analyst, 'GET', '/samples').json()}
    worker('start'); m7 = wait_merge(analyst, m7['id'], 'completed', timeout=90); worker('stop')
    after = api(analyst, 'GET', '/samples').json()
    check('S7.3 恢复后迁完且核对一致', m7['moved'] == 120 and m7['remaining'] == 0)
    check('S7.4 恢复不重移不丢样（总数与编号集合不变）',
          len(after) == len(before_codes) and {s['code'] for s in after} == before_codes
          and len(samples_of(analyst, big['id'])) == 0)

    # ---------- S8 并发收样/修订/评议 不丢样不混组 ----------
    conc = mkprocess(analyst, 'VM-CONC', '并发旧档')
    mksamples(analyst, conc['id'], 'VCC', 60)
    m8 = api(analyst, 'POST', '/merges', json={'source_id': conc['id'], 'target_id': s2['id']}).json()
    s2_before = len(samples_of(analyst, s2['id']))
    def post_sample(i):
        tgt = conc['id'] if i % 2 == 0 else s2['id']  # 一半投别名一半投目标
        return api(analyst, 'POST', '/samples', json={'code': f'VCC-NEW-{i:02}', 'process_id': tgt, 'lot': 'VM-02', 'force': 25.0})
    with ThreadPoolExecutor(6) as ex:
        futures = [ex.submit(post_sample, i) for i in range(12)]
        futures.append(ex.submit(api, analyst, 'PUT', f"/processes/{s2['id']}",
                                 json={'code': 'HC-S2', 'name': '延时封合', 'temperature': 162.0, 'dwell': 1.2, 'pressure': 0.32, 'note': '归并期间修订参数'}))
        results = [f.result() for f in futures]
    check('S8.1 归并期间收样全部成功且归入目标',
          all(r.status_code == 201 and r.json()['process_id'] == s2['id'] for r in results[:12]))
    check('S8.2 归并期间可修订目标参数', results[12].status_code == 200 and results[12].json()['temperature'] == 162.0)
    # 收样/修订并发落定后、迁移仍在进行中时创建评议：源与目标同选按一个工艺分析
    r8 = api(analyst, 'POST', '/assessments', json={'title': 'VM 归并中评议', 'process_ids': [conc['id'], s2['id'], s3['id']], 'lot': '全部'})
    a8 = r8.json()
    g8 = [g for g in a8['snapshot']['groups'] if g['code'] == 'HC-S2'][0]
    check('S8.3 归并中评议：源与目标同选按一个工艺、不混用前后分组',
          len(a8['snapshot']['groups']) == 2 and 'VM-CONC' in g8['aliases']
          and len(g8['samples']) == s2_before + 60 + 12)
    worker('start'); m8 = wait_merge(analyst, m8['id'], 'completed', timeout=90); worker('stop')
    check('S8.4 并发后核对一致收工', m8['moved'] == 60 and m8['remaining'] == 0)
    check('S8.5 并发收样零丢失', len(samples_of(analyst, s2['id'])) == s2_before + 60 + 12)

    # ---------- S9 迁移中修订样本：离开源工艺计入迁出 ----------
    ed = mkprocess(analyst, 'VM-EDIT', '待修订旧档')
    mksamples(analyst, ed['id'], 'VED', 4)
    m9 = api(analyst, 'POST', '/merges', json={'source_id': ed['id'], 'target_id': s1['id']}).json()
    victim = samples_of(analyst, ed['id'])[0]
    r = api(analyst, 'PUT', f"/samples/{victim['id']}",
            json={'code': victim['code'], 'process_id': ed['id'], 'lot': 'VM-01', 'force': 29.9, 'flagged': True, 'note': '迁移中修订'})
    check('S9.1 迁移中修订样本被重定向到目标', r.status_code == 200 and r.json()['process_id'] == s1['id'] and r.json()['force'] == 29.9)
    worker('start'); m9 = wait_merge(analyst, m9['id'], 'completed'); worker('stop')
    check('S9.2 修订离开的样本计入迁出，核对一致', m9['moved'] == 4 and m9['remaining'] == 0)

    # ---------- S10 数量核对不一致 → failed → 重试不重复移动 ----------
    fx = mkprocess(analyst, 'VM-FAIL', '核对失败旧档')
    mksamples(analyst, fx['id'], 'VFX', 3)
    m10 = api(analyst, 'POST', '/merges', json={'source_id': fx['id'], 'target_id': s1['id']}).json()
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute('UPDATE process_merges SET expected_total=expected_total+1 WHERE id=%s', (m10['id'],))
    worker('start'); m10 = wait_merge(analyst, m10['id'], 'failed'); worker('stop')
    check('S10.1 数量核对不一致判失败', '数量核对不一致' in m10['error'])
    codes_before = {s['code'] for s in api(analyst, 'GET', '/samples').json()}
    r = api(admin, 'POST', f"/merges/{m10['id']}/retry")
    check('S10.2 失败归并可重试', r.status_code == 200 and r.json()['status'] == 'migrating')
    worker('start'); m10 = wait_merge(analyst, m10['id'], 'completed'); worker('stop')
    after10 = api(analyst, 'GET', '/samples').json()
    check('S10.3 重试不重复移动样本（编号集合与总数不变）',
          m10['moved'] == 0 and len(after10) == len(codes_before) and {s['code'] for s in after10} == codes_before)

    # ---------- S11 权限 ----------
    check('S11.1 viewer 可看归并进度', api(viewer, 'GET', '/merges').status_code == 200)
    check('S11.2 viewer 可预览（只读）', api(viewer, 'POST', '/merges/preview', json={'source_id': s2['id'], 'target_id': s3['id']}).status_code == 200)
    check('S11.3 viewer 不能发起归并', api(viewer, 'POST', '/merges', json={'source_id': s2['id'], 'target_id': s3['id']}).status_code == 403)

    # ---------- S12 同对并发重复发起 ----------
    d1 = mkprocess(analyst, 'VM-D1', '双发A'); d2 = mkprocess(analyst, 'VM-D2', '双发B')
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(lambda _: api(analyst, 'POST', '/merges', json={'source_id': d1['id'], 'target_id': d2['id']}), range(2)))
    check('S12.1 同一对并发发起恰好一个成功', sorted(r.status_code for r in rs) == [201, 409])
    mid = [r.json()['id'] for r in rs if r.status_code == 201][0]
    worker('start'); wait_merge(analyst, mid, 'completed'); worker('stop')

    # ---------- S13 目标为别名时归一到最终工艺 ----------
    nx = mkprocess(analyst, 'VM-NX', '投向别名的新档')
    mksamples(analyst, nx['id'], 'VNX', 2)
    prev = api(analyst, 'POST', '/merges/preview', json={'source_id': nx['id'], 'target_id': dup['id']}).json()
    check('S13.1 预览提示目标是别名并落到最终工艺', prev['ok'] and prev['target_is_alias'] and prev['target']['code'] == 'HC-S1')
    m13 = api(analyst, 'POST', '/merges', json={'source_id': nx['id'], 'target_id': dup['id']}).json()
    check('S13.2 归并目标归一为最终工艺', m13['canonical_id'] == s1['id'] and m13['target_id'] == s1['id'])
    worker('start'); wait_merge(analyst, m13['id'], 'completed'); worker('stop')

    # ---------- 审计留痕 ----------
    audit = api(admin, 'GET', '/audit').json()
    check('S14 归并动作进入操作审计', any('POST /merges' == a['action'] for a in audit))

    failed = [c for c in checks if not c[1]]
    print(f"\n===== {len(checks) - len(failed)}/{len(checks)} 通过 =====")
    if failed:
        for name, _, detail in failed:
            print('FAILED:', name, detail)
        sys.exit(1)

if __name__ == '__main__':
    main()

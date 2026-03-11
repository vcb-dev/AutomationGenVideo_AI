"""
Load Test - 30 concurrent users
Test target: https://automation-gen-video-pkmxlaplu-truqhieus-projects.vercel.app/
"""
import concurrent.futures
import urllib.request
import time
import statistics

BASE_URL = "https://automation-gen-video-pkmxlaplu-truqhieus-projects.vercel.app/"

results = []

def test_request(user_id):
    import urllib.error
    url = BASE_URL
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': f'LoadTest-User-{user_id}'
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            elapsed = time.time() - start
            return {
                'user': user_id,
                'status': resp.status,
                'time': round(elapsed, 3),
                'size_kb': round(len(body) / 1024, 1),
                'ok': True
            }
    except urllib.error.HTTPError as e:
        elapsed = time.time() - start
        return {'user': user_id, 'status': e.code, 'time': round(elapsed, 3), 'ok': False, 'error': str(e)}
    except Exception as e:
        elapsed = time.time() - start
        return {'user': user_id, 'status': 0, 'time': round(elapsed, 3), 'ok': False, 'error': str(e)[:60]}

print(f"Starting load test: 30 concurrent users -> {BASE_URL}")
print("=" * 60)
start_all = time.time()

with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
    futures = [executor.submit(test_request, i+1) for i in range(30)]
    for f in concurrent.futures.as_completed(futures):
        r = f.result()
        results.append(r)
        icon = "OK" if r['ok'] else "FAIL"
        err = f" | {r.get('error','')}" if not r['ok'] else f" | {r.get('size_kb',0)} KB"
        print(f"  [{icon}] User {r['user']:2d}: HTTP {r['status']} | {r['time']}s{err}")

total_time = round(time.time() - start_all, 2)
ok_results = [r for r in results if r['ok']]
fail_results = [r for r in results if not r['ok']]
times = [r['time'] for r in ok_results]

print(f"\n{'='*60}")
print(f"LOAD TEST RESULTS")
print(f"{'='*60}")
print(f"  Total requests : 30")
print(f"  Success        : {len(ok_results)}")
print(f"  Failed         : {len(fail_results)}")
print(f"  Total wall time: {total_time}s")

if times:
    print(f"\n  Response Times:")
    print(f"     Min    : {min(times)}s")
    print(f"     Max    : {max(times)}s")
    print(f"     Average: {round(statistics.mean(times), 3)}s")
    print(f"     Median : {round(statistics.median(times), 3)}s")
    if len(times) > 1:
        p90 = sorted(times)[int(len(times)*0.9)]
        print(f"     P90    : {round(p90, 3)}s")

print(f"\n  Throughput: {round(30/total_time, 1)} req/s")

if len(ok_results) == 30:
    if statistics.mean(times) < 3:
        print(f"\n  KET LUAN: Server CHIU TAI TOT voi 30 users!")
    else:
        print(f"\n  KET LUAN: Server phan hoi CHAM (>3s avg) voi 30 users")
elif len(ok_results) >= 25:
    print(f"\n  KET LUAN: Server CON CHIU DUOC nhung co {len(fail_results)} request that bai")
else:
    print(f"\n  KET LUAN: Server KHONG CHIU TAI duoc 30 users ({len(fail_results)} that bai)")

"""Browser checks for the real frontend with intercepted synthetic API responses.

No server, database, account profile or upstream platform is opened. Every
browser request is intercepted; anything outside the fixture origin is refused.
Run manually with the existing Playwright dependency and a Chromium executable.
"""
from __future__ import annotations
import argparse
import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'http://127.0.0.1:18766'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-executable', required=True)
    parser.add_argument('--output', default='.cache/frontend-mock')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    subscriptions = [
        dict(platform='xiaohongshu', author_id='fixture-a', display_name='示例 · 山间手记', item_count=3, detail_count=2, identity_verified=True, subscribed=True, enabled=True, source_connected=True, source_configured=True, source_kind='xhs_http', tags=['旅行'], registered_media=dict(image=2, video=1)),
        dict(platform='xiaohongshu', author_id='fixture-b', display_name='示例 · 日常摄影', item_count=1, detail_count=0, identity_verified=True, subscribed=True, enabled=True, source_connected=True, source_configured=True, source_kind='xhs_http', tags=['摄影']),
        dict(platform='wechat', author_id='fixture-c', display_name='示例 · 城市阅读', item_count=1, detail_count=1, identity_verified=True, subscribed=True, enabled=True, source_connected=False, source_configured=False, tags=['阅读']),
    ]
    items = [
        dict(platform='xiaohongshu', author_id='fixture-a', item_id='image-a', title='山间的一天：把路上的风景留在本机', content_type='image', archive_status='complete', published_at='2026-10-01T08:00:00Z'),
        dict(platform='xiaohongshu', author_id='fixture-a', item_id='video-a', title='沿着溪流走一段 · 视频记录', content_type='video', archive_status='partial', published_at='2026-10-01T07:00:00Z'),
        dict(platform='xiaohongshu', author_id='fixture-a', item_id='missing-a', title='夜色与远方 · 正文尚未保存', content_type='image', archive_status='missing', published_at='2026-09-30T08:00:00Z'),
        dict(platform='xiaohongshu', author_id='fixture-b', item_id='image-b', title='早晨的窗边光线', content_type='image', archive_status='missing', published_at='2026-10-01T06:00:00Z'),
        dict(platform='wechat', author_id='fixture-c', item_id='wechat-a', title='留一段安静的阅读时间', content_type='image', archive_status='complete', published_at='2026-09-29T08:00:00Z'),
    ]
    workspace = dict(subscriptions=subscriptions, runs=[], archive_batches=[], platforms=[], refresh_schedule=dict(enabled=False, interval_minutes=1440), stats=dict(items=5, details=3, assets=3), build=dict(version='mock-ui', commit='synthetic'), xhs_account=dict(state='disconnected'), data_dir='模拟资料 / workspace', archive_dir='模拟资料 / archive', obsidian_dir='未设置')
    writes, intercepted, refused, errors = [], [], [], []
    controls = dict(subscription_error=True, hold_jobs=False, held=[])
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540"><rect width="960" height="540" fill="#e5eee7"/><circle cx="740" cy="105" r="48" fill="#e8be6b"/><path d="M0 430L245 155L450 405L670 205L960 430V540H0Z" fill="#7d9b85"/><path d="M0 490L320 295L600 500L810 360L960 490V540H0Z" fill="#476c60"/><text x="40" y="65" font-size="26" fill="#28493c">LOCAL FIXTURE · 本地模拟图片</text></svg>'''
    video = b''

    def fulfill_json(route, data, status=200):
        route.fulfill(status=status, content_type='application/json; charset=utf-8', body=json.dumps(data, ensure_ascii=False))

    def route_request(route):
        nonlocal video
        request = route.request
        url = urlparse(request.url)
        intercepted.append(url.path)
        if f'{url.scheme}://{url.netloc}' != ORIGIN:
            refused.append(request.url)
            route.abort()
            return
        path = url.path
        if path == '/':
            route.fulfill(content_type='text/html; charset=utf-8', body=(ROOT/'creator_archive/static/index.html').read_bytes())
        elif path in ['/static/app.js', '/static/style.css']:
            route.fulfill(content_type='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8', body=(ROOT/'creator_archive'/path.lstrip('/')).read_bytes())
        elif path == '/fixture/image.svg':
            route.fulfill(content_type='image/svg+xml', body=svg)
        elif path == '/fixture/video.webm':
            route.fulfill(content_type='video/webm', body=video)
        elif path == '/api/workspace':
            fulfill_json(route, workspace)
        elif path == '/api/items':
            query = parse_qs(url.query)
            matching = [item for item in items if all(not query.get(key) or item[key] == query[key][0] for key in ('platform', 'author_id'))]
            if query.get('text'):
                matching = [item for item in matching if query['text'][0] in item['title']]
            fulfill_json(route, dict(items=matching, total=len(matching)))
        elif path.startswith('/api/items/'):
            item = next(item for item in items if path.endswith('/'+item['item_id']))
            missing = item['archive_status'] == 'missing'
            asset = dict(asset_id='fixture-media', kind='video' if item['content_type'] == 'video' else 'image', state='complete', url='/fixture/video.webm' if item['content_type'] == 'video' else '/fixture/image.svg')
            fulfill_json(route, dict(item=dict(**item, detail_text='' if missing else '这是一份离线模拟正文。\n\n沿着山路慢慢走，记录风、树影与溪流。正文、图片和视频在阅读时只从本机读取。\n\n示例数据不代表真实平台采集成功。', detail_state='missing' if missing else 'complete', media_state='partial' if missing or item['archive_status']=='partial' else 'complete_for_observed_detail', assets=[] if missing else [asset])))
        elif request.method == 'POST':
            body = request.post_data_json
            writes.append(dict(path=path, body=body))
            if path == '/api/subscriptions' and controls['subscription_error']:
                fulfill_json(route, dict(message='平台业务请求失败；已停止当次任务，旧资料保留', business_code=-100), 409)
            elif path == '/api/jobs':
                if controls['hold_jobs']:
                    controls['held'].append(route)
                    return
                job_id = len(writes) + 100
                author = next((s for s in subscriptions if s['author_id'] == body.get('author_id')), subscriptions[0])
                workspace['runs'] = [dict(id=job_id, mode='recent_window' if body['mode']=='source_refresh' else body['mode'], platform=author['platform'], author_id=author['author_id'], display_name=author['display_name'], state='partial' if body['mode']=='source_refresh' else 'running', reason='recent_window_partial' if body['mode']=='source_refresh' else '', window_size=30, target_count=3, item_count=1, body_saved_count=1, registered_media=dict(image=1, video=0), media_partial_item_count=1, updated_at=1791000000)]
                fulfill_json(route, dict(job_ids=[job_id], message='模拟任务已创建，请查看实际进度'))
            else:
                fulfill_json(route, dict(message='模拟操作'))
        else:
            fulfill_json(route, dict(detail='Unexpected fixture request'), 404)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.browser_executable, headless=True, args=['--disable-background-networking'])
        context = browser.new_context(viewport=dict(width=1440, height=1000), service_workers='block', reduced_motion='reduce')
        context.route('**/*', route_request)
        # Generate a tiny playable video entirely in this temporary browser context.
        recorder = context.new_page()
        encoded = recorder.evaluate('''async () => {
          const canvas=document.createElement('canvas');canvas.width=480;canvas.height=270;
          const ctx=canvas.getContext('2d');ctx.fillStyle='#476c60';ctx.fillRect(0,0,480,270);
          ctx.fillStyle='#ffffff';ctx.font='24px sans-serif';ctx.fillText('LOCAL VIDEO FIXTURE',75,140);
          const stream=canvas.captureStream(10);const chunks=[];const recording=new MediaRecorder(stream,{mimeType:'video/webm'});
          const done=new Promise(resolve=>recording.onstop=resolve);recording.ondataavailable=event=>chunks.push(event.data);
          recording.start();let frame=0;const drawing=setInterval(()=>{ctx.fillStyle='#476c60';ctx.fillRect(0,0,480,270);ctx.fillStyle='#ffffff';ctx.fillText('LOCAL VIDEO FIXTURE '+(++frame),60,140);},80);await new Promise(resolve=>setTimeout(resolve,1100));clearInterval(drawing);recording.stop();await done;stream.getTracks().forEach(track=>track.stop());
          return await new Promise(resolve=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.readAsDataURL(new Blob(chunks,{type:'video/webm'}));});
        }''')
        video = base64.b64decode(encoded)
        recorder.close()
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        def capture(name):
            page.evaluate('document.activeElement.blur();window.scrollTo({top:0,left:0,behavior:"instant"})')
            page.screenshot(path=str(output/name), full_page=True)
        page.goto(ORIGIN)
        expect(page.locator('#item-list .item-row')).to_have_count(5)
        expect(page.locator('#reader-all')).to_have_attribute('aria-pressed', 'true')
        expect(page.locator('#reader-author-actions button').first).to_be_disabled()
        assert writes == []
        capture('overview.png')
        page.click('#reader-xhs')
        expect(page.locator('#item-list .item-row')).to_have_count(4)
        page.click('#reader-wechat')
        expect(page.locator('#item-list .item-row')).to_have_count(1)
        page.click('#reader-all')
        page.locator('.reader-author').filter(has_text='示例 · 山间手记').click()
        expect(page.locator('#item-list .item-row')).to_have_count(3)
        expect(page.locator('#reader-scope')).to_contain_text('当前博主：示例 · 山间手记')
        expect(page.locator('#reader-title')).to_have_text('示例 · 山间手记')
        assert writes == []
        page.get_by_role('button', name='山间的一天：把路上的风景留在本机').click()
        expect(page.locator('#item-dialog pre')).to_contain_text('离线模拟正文')
        page.wait_for_function("document.querySelector('#item-dialog img').naturalWidth > 0")
        expect(page.locator('#item-dialog .reader-advanced')).not_to_have_attribute('open', '')
        capture('reading.png')
        page.click('#close-detail')
        page.get_by_role('button', name='沿着溪流走一段 · 视频记录').click()
        page.wait_for_function("document.querySelector('#item-dialog video').readyState >= 1")
        page.locator('#item-dialog video').evaluate('(video)=>{video.loop=true;video.muted=true;return video.play();}')
        page.wait_for_function("!document.querySelector('#item-dialog video').paused")
        expect(page.locator('#item-dialog')).to_contain_text('已知媒体部分缺失')
        page.click('#close-detail')
        page.get_by_role('button', name='夜色与远方 · 正文尚未保存').click()
        expect(page.locator('#item-dialog')).to_contain_text('正文尚未保存')
        page.click('#close-detail')
        # Cancel via Escape, then force a semantic platform failure in the same dialog.
        page.click('#toolbar-add')
        expect(page.locator('#sync-window-limit')).to_have_value('3')
        page.keyboard.press('Escape')
        assert writes == []
        page.click('#toolbar-add')
        page.fill('#link', 'https://www.xiaohongshu.com/user/profile/fixture-author')
        page.get_by_role('button', name='订阅并同步', exact=True).click()
        expect(page.locator('#subscribe-feedback')).to_contain_text('-100')
        expect(page.locator('#add-author-dialog')).to_be_visible()
        assert writes[-1]['body']['window_limit'] == 3
        controls['subscription_error'] = False
        page.get_by_role('button', name='订阅并同步', exact=True).click()
        expect(page.locator('#add-author-dialog')).not_to_be_visible()
        expect(page.locator('#main')).to_have_attribute('data-view', 'jobs')
        page.locator('nav a[href="#library"]').click()
        page.get_by_role('button', name='更新当前博主', exact=True).click()
        expect(page.locator('#job-list')).to_contain_text('部分完成')
        assert writes[-1]['body'] == dict(mode='source_refresh', platform='xiaohongshu', author_id='fixture-a')
        capture('partial-task.png')
        page.locator('nav a[href="#library"]').click()
        controls['hold_jobs'] = True
        page.get_by_role('button', name='下载当前博主', exact=True).click()
        expect(page.locator('#reader-author-actions button').nth(1)).to_be_disabled()
        write_count = len(writes)
        page.get_by_role('button', name='更新当前博主', exact=True).click()
        expect(page.locator('#feedback')).to_contain_text('上一操作处理中')
        assert len(writes) == write_count
        controls['hold_jobs'] = False
        for route in controls['held']:
            fulfill_json(route, dict(job_ids=[123], message='模拟本机导出已创建'))
        expect(page.locator('#main')).to_have_attribute('data-view', 'jobs')
        assert writes[-1]['body'] == dict(mode='archive', platform='xiaohongshu', author_id='fixture-a')
        page.locator('nav a[href="#library"]').click()
        page.locator('#reader-bulk summary').click()
        page.click('#update-all')
        expect(page.locator('#main')).to_have_attribute('data-view', 'jobs')
        assert writes[-1]['body'] == dict(mode='source_refresh')
        page.locator('nav a[href="#library"]').click()
        page.click('#download-all')
        expect(page.locator('#main')).to_have_attribute('data-view', 'jobs')
        assert writes[-1]['body'] == dict(mode='archive')
        # Explicitly clear management selection on platform/tag changes.
        page.locator('nav a[href="#library"]').click()
        expect(page.locator('#main')).to_have_attribute('data-view', 'library')
        page.locator('nav a[href="#sources"]').click()
        page.go_back()
        expect(page.locator('#main')).to_have_attribute('data-view', 'library')
        page.go_forward()
        expect(page.locator('#main')).to_have_attribute('data-view', 'sources')
        expect(page.locator('#reader-account')).to_be_visible()
        page.click('#open-subscriptions')
        page.select_option('#subscription-platform', '')
        page.locator('.select-author input').first.check()
        expect(page.locator('#selected-archive-count')).to_contain_text('已选 1 位')
        page.select_option('#subscription-platform', 'wechat')
        expect(page.locator('#selected-archive-count')).to_contain_text('已选 0 位')
        page.click('#manage-back')
        page.locator('nav a[href="#sources"]').click()
        page.click('#reader-account')
        before = len(writes)
        page.click('#close-account')
        assert len(writes) == before
        capture('settings.png')
        page.locator('nav a[href="#library"]').click()
        page.click('#reader-xhs')
        page.fill('#reader-search', '找不到的模拟博主')
        expect(page.locator('#reader-authors')).to_contain_text('没有匹配的博主')
        page.fill('#reader-search', '')
        page.locator('#reader-filter-details summary').click()
        page.fill('#filter-text', '不存在的模拟作品')
        page.get_by_role('button', name='应用全库筛选').click()
        expect(page.locator('#item-list')).to_contain_text('没有符合条件的作品')
        page.click('#reader-all')
        page.locator('#reader-filter-details summary').click()
        page.set_viewport_size(dict(width=768, height=1024))
        expect(page.locator('.reader-sidebar')).to_have_css('position', 'fixed')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Tablet layout overflows'
        capture('tablet.png')
        page.locator('nav a[href="#sources"]').click()
        page.select_option('#theme', 'dark')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        capture('dark-settings.png')
        page.select_option('#theme', 'light')
        page.locator('nav a[href="#library"]').click()
        page.set_viewport_size(dict(width=390, height=844))
        expect(page.locator('#item-list .item-row')).to_have_count(5)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile layout overflows'
        capture('mobile.png')
        assert not errors, errors
        assert not refused, refused
        context.close()
        browser.close()
    report = dict(result='passed', backend_started=False, real_platform_requests=0, refused_requests=len(refused), javascript_errors=errors, intercepted_requests=len(intercepted), synthetic_writes=writes, checks=['platform/all/author scope', 'local image and playable video', 'missing body and partial media', 'subscription cancel and business failure', 'current author update/export', 'all subscription update/export', 'busy/double click', 'selection reset', 'account close without request', 'empty states', 'desktop/tablet/mobile/dark screenshots'])
    (output/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(result='passed', screenshots=str(output), real_platform_requests=0, checks=len(report['checks'])), ensure_ascii=False))


if __name__ == '__main__':
    main()

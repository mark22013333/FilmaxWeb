"""Player controls in installed Chromium/Edge with native video volume events."""
import json
import os
import sys
from pathlib import Path
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
INFO = {
    "file_id": 1, "title": "播放器測試", "filename": "Reacher.S01E01.mp4",
    "mode": "hls", "probe_state": "ok", "duration": 3600, "resume": 0,
    "width": 1920, "height": 1080, "remote": False,
    "hls_url": "/empty.m3u8", "direct_url": "/empty.mp4",
    "quality": {"ladder": []}, "audio_tracks": [], "subtitles": [],
    "next_episode": {"file_id": 2, "label": "S01E02", "title": "第二集", "url": "/player?file=2"},
}
HLS = """
window.Hls = class {
 constructor(){this._h={};this.levels=[];this.autoLevelEnabled=true;}
 on(e,f){(this._h[e]||=[]).push(f);} loadSource(){} attachMedia(){} startLoad(){}
 stopLoad(){} detachMedia(){} destroy(){} recoverMediaError(){} swapAudioCodec(){}
};
Hls.isSupported=()=>true;
Hls.Events={MANIFEST_PARSED:'manifest',LEVEL_SWITCHING:'switching',LEVEL_SWITCHED:'switched',
 FRAG_LOADING:'loading',FRAG_LOADED:'loaded',FRAG_BUFFERED:'buffered',ERROR:'error'};
Hls.ErrorTypes={NETWORK_ERROR:'network',MEDIA_ERROR:'media'};
"""

class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split('?')[0]
        if path == '/player':
            return str(ROOT / 'app/static/player.html')
        if path.startswith('/static/'):
            return str(ROOT / 'app' / path.lstrip('/'))
        return str(ROOT / 'data/nonexistent-test-resource')

    def log_message(self, *_args):
        pass

class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

server = Server(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}'
checks = 0

def check(value, label):
    global checks
    assert value, label
    checks += 1

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.getenv('FILMAX_BROWSER_CHANNEL', 'msedge'))
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        context.route('**/static/hls.min.js', lambda route: route.fulfill(content_type='application/javascript', body=HLS))
        requests = []
        def api(route):
            path = route.request.url.split('/api/')[1]
            if path == 'me':
                body = {"is_admin": False}
            elif path.startswith('play/'):
                body = dict(INFO)
                if path.split('/')[1].split('?')[0] == '2':
                    body['next_episode'] = None
            elif path == 'progress':
                requests.append(route.request.post_data_json)
                body = {"ok": True}
            else:
                body = {"data": {}, "updated_at": 0}
            route.fulfill(content_type='application/json', body=json.dumps(body))
        context.route('**/api/**', api)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/player?file=1')
        page.wait_for_selector('#btnNextEp:not([hidden])')
        check(page.locator('#nextEp').is_hidden(), 'floating next stays hidden early')
        for volume, state in [(0, 'muted'), (.2, 'low'), (.5, 'low'), (.8, 'high')]:
            page.evaluate('(value)=>{document.querySelector("#v").volume=value}', volume)
            page.wait_for_function('(state)=>document.querySelector("#btnMute").dataset.volumeState===state', arg=state)
            check(state == page.locator('#btnMute').get_attribute('data-volume-state'), 'native volumechange updates icon')
        page.locator('#btnMute').click()
        page.wait_for_function('document.querySelector("#btnMute").dataset.volumeState === "muted"')
        check('取消靜音' in page.locator('#btnMute').get_attribute('aria-label'), 'mute action accessible')
        page.locator('#btnMute').click()
        page.locator('#btnSettings').click()
        brightness = page.get_by_label('亮度', exact=True)
        brightness.evaluate('(el)=>{el.value=150;el.dispatchEvent(new Event("input",{bubbles:true}))}')
        check(page.locator('#v').evaluate('(el)=>getComputedStyle(el).filter').startswith('brightness(1.5)'), 'brightness live on video')
        check(page.locator('#subs').evaluate('(el)=>getComputedStyle(el).filter') == 'none', 'subtitle brightness unaffected')
        check(page.locator('#ctl').evaluate('(el)=>getComputedStyle(el).filter') == 'none', 'control brightness unaffected')
        check('brightness' not in json.loads(page.evaluate('localStorage.getItem("filmax.player") || "{}"')), 'display values do not sync with player prefs')
        brightness.focus()
        current_time = page.locator('#v').evaluate('(el)=>el.currentTime')
        page.keyboard.press('ArrowLeft')
        check(page.locator('#v').evaluate('(el)=>el.currentTime') == current_time, 'slider arrow does not seek video')
        page.reload()
        page.wait_for_selector('#btnNextEp:not([hidden])')
        check(page.locator('#v').evaluate('(el)=>getComputedStyle(el).filter').startswith('brightness(1.49)'), 'display choice survives reload')
        page.locator('#btnSettings').click()
        page.get_by_role('button', name='恢復畫面預設值').click()
        check(page.locator('#v').evaluate('(el)=>getComputedStyle(el).filter') == 'none', 'reset removes filter')
        for width, height in [(320, 720), (390, 844), (844, 390)]:
            page.set_viewport_size({"width": width, "height": height})
            check(page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'no overflow at {width}')
            bounds = page.locator('#btnNextEp').bounding_box()
            check(bounds is not None and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width, f'next control within {width}')
        page.set_viewport_size({"width": 1280, "height": 800})
        page.locator('#btnSettings').click()
        page.locator('#v').evaluate('(el)=>{el.currentTime=120;el.dispatchEvent(new Event("timeupdate"))}')
        # 跳到 120 會觸發 seeked 的進度寫入；sync API 要到下一次 wait 才處理 route，
        # 不等它落地就 clear 的話，它會在 clear 之後才被記到，看起來像換集存了兩次。
        for _ in range(40):
            if any(r.get('position') == 120 for r in requests):
                break
            page.wait_for_timeout(50)
        requests.clear()
        page.locator('#btnNextEp').focus()
        page.keyboard.press('Space')
        page.wait_for_url('**/player?file=2')
        page.wait_for_selector('#btnNextEp[hidden]', state='attached')
        check(len(requests) == 1 and requests[0]['finished'] is False and requests[0]['position'] == 120, 'keyboard next saves early progress once before navigation')
        check(page.locator('#btnNextEp').is_hidden(), 'last episode hides next button')
        for raw in ['null', '[]', 'broken', '{"brightness":"150"}']:
            page.evaluate('(raw)=>localStorage.setItem("filmax.player.visual",raw)', raw)
            page.reload()
            page.wait_for_selector('#btnSettings')
            check(page.locator('#v').evaluate('(el)=>getComputedStyle(el).filter') == 'none', 'invalid saved visual defaults')
        check(not errors, 'no JavaScript exceptions: ' + repr(errors))
        browser.close()
finally:
    server.shutdown()
    server.server_close()
print(f'Player browser controls: {checks} checks passed')

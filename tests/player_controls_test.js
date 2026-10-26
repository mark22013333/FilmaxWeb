
'use strict';
const assert = require('assert');
const {load, start} = require('./player_harness');
let checks = 0;
function check(value, message) { assert(value, message); checks++; }
function context(stored) {
  return load({ beforeRun(sandbox) {
    if (stored !== undefined) sandbox.localStorage.setItem('filmax.player.visual', stored);
  }});
}
async function main() {
  const ctx = context();
  const icons = [];
  for (const [volume, state] of [[0, 'muted'], [.2, 'low'], [.5, 'low'], [.8, 'high']]) {
    ctx.video.volume = volume; ctx.video.dispatch('volumechange');
    check(ctx.get('btnMute').dataset.volumeState === state, `volume ${volume} uses ${state}`);
    check(ctx.get('btnMute').getAttribute('aria-label').includes(Math.round(volume * 100) + '%'), 'volume accessible label');
    icons.push(ctx.get('icVol').innerHTML);
  }
  check(icons[0] !== icons[1] && icons[1] !== icons[3], 'mute, low and high icons differ');
  ctx.get('btnMute').onclick(); check(ctx.get('btnMute').dataset.volumeState === 'muted', 'mute button updates icon');
  ctx.get('btnMute').onclick(); check(ctx.get('btnMute').dataset.volumeState === 'high', 'unmute restores high icon');
  check(ctx.video.style.filter === 'none', 'default video has no filter');
  const visual = context(JSON.stringify({brightness: 140, contrast: 85, saturation: 120}));
  check(visual.video.style.filter === 'brightness(140%) contrast(85%) saturate(120%)', 'stored display settings applied');
  check(!('brightness' in visual.hooks.cfg), 'display preferences do not enter synced cfg');
  for (const raw of ['null', '[]', 'broken', '{"brightness":"150","contrast":null}']) {
    check(context(raw).video.style.filter === 'none', 'malformed preferences default safely');
  }
  check(context('{"brightness":999,"contrast":-1,"saturation":-1}').video.style.filter ===
    'brightness(200%) contrast(50%) saturate(0%)', 'stored values clamp');
  const noNext = context(); start(noNext, 'direct'); noNext.hooks.syncNextEp();
  check(noNext.get('btnNextEp').hidden, 'movie or last episode hides toolbar next');
  for (const finish of [false, true]) {
    const next = context();
    start(next, 'direct', {next_episode: {file_id: 42, label: 'S02E01', title: 'Next'}});
    next.video.currentTime = 120; next.hooks.syncNextEp();
    check(!next.get('btnNextEp').hidden, 'toolbar next visible early');
    check(next.get('nextEp').hidden, 'late overlay stays hidden early');
    let release;
    const calls = [], beacons = [];
    next.sandbox.fetch = (url, options) => {
      if (url === '/api/progress') {
        calls.push(JSON.parse(options.body));
        return new Promise(resolve => { release = resolve; });
      }
      return Promise.reject(new Error('unexpected request'));
    };
    next.sandbox.navigator.sendBeacon = (_url, blob) => { beacons.push(blob._text); return true; };
    const navigation = next.hooks.goNextEpisode(finish);
    next.hooks.goNextEpisode(finish);
    check(calls.length === 1, 'rapid next clicks send only one progress request');
    check(calls[0].finished === finish && calls[0].position === 120, 'early skip preserves unfinished progress; overlay completes');
    check(next.sandbox.location.href === '', 'navigation awaits saved progress');
    next.video.dispatch('pause'); next.sandbox.window.dispatch('pagehide'); next.sandbox.window.dispatch('beforeunload');
    next.sandbox.document.visibilityState = 'hidden'; next.sandbox.document.dispatch('visibilitychange');
    check(beacons.length === 0, 'lifecycle cannot overwrite navigation progress');
    check(next.get('nextEp').disabled && next.get('btnNextEp').disabled, 'both next buttons disabled while saving');
    release({ok: true}); await navigation;
    check(next.sandbox.location.href === '/player?file=42', 'next URL matches backend');
    next.sandbox.window.dispatch('pageshow');
    check(!next.get('btnNextEp').disabled && !next.hooks.feedbackMuted, 'back navigation restores controls');
  }
  console.log(`Player controls: ${checks} checks passed`);
}
main().then(() => process.exit(0)).catch(error => {console.error(error); process.exit(1);});

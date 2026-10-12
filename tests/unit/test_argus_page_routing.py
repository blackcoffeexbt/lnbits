"""Exercise LNbits' real fallback router against the extension's canonical URL."""

import subprocess
from pathlib import Path
from shutil import which


def test_argus_canonical_url_does_not_reload_without_dynamic_routes():
    root = Path(__file__).resolve().parents[2]
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('lnbits/static/js/init-app.js', 'utf8')
  .split('const routes = [')[0];
async function redirects(path) {
  let navigations = [];
  const window = {};
  Object.defineProperty(window, 'location', {set(value) {navigations.push(value)}});
  const sandbox = {
    window, RENDERED_ROUTE: '/argus', Quasar: {}, console: {log() {}},
    fetch: async () => ({ok: false}) // Legacy extension has no routes.json.
  };
  vm.runInNewContext(source + '; this.handler = DynamicComponent.created;', sandbox);
  await sandbox.handler.call({
    $route: {path, fullPath: path}, $router: {getRoutes: () => []}
  });
  await new Promise(setImmediate);
  return navigations;
}
(async () => {
  assert.deepEqual(await redirects('/argus/'), []);
  assert.deepEqual(await redirects('/argus'), ['/argus']); // Original loop trigger.
})().catch(error => {console.error(error);process.exitCode = 1});
"""
    node = which("node")
    assert node is not None
    # Execute only the fixed repository test script, with no user input.
    subprocess.run([node, "-e", script], cwd=root, check=True, timeout=10)  # noqa: S603

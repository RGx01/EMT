"""
The setup wizard keeps a battery's Solar / PV power sensor (4 Nov 2026, the user, a scratch setup).

The wizard's Device Sensors step draws a "Solar / PV Power Sensor" field for a battery
(`wiz-sub-battery-pv-power`), and its finish writes `sd.pv_power` to the device's
`pv_power_sensor` — but `wizCollectStep` never read the field back, so `sd.pv_power` was never set
and the sensor was dropped: the device's card in Setup showed it empty.

This runs the page's real `wizCollectStep` and `wizApplyAndClose` under node, with the step's fields
filled, and reads the device the finish writes. Skipped where node is unavailable.

Discriminating: test_the_pv_sensor_is_kept fails on the parent; the GUARD pins the battery's other
sensors, which were already read back.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "web", "templates", "meter_config.html")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const TEMPLATE = process.argv[2];
let html = fs.readFileSync(TEMPLATE, 'utf8');
let js = (html.match(/<script[^>]*>([\s\S]*?)<\/script>/g) || [])
  .map(s => s.replace(/<\/?script[^>]*>/g, '')).join('\n');
js = js.replace(/\{\{[\s\S]*?\}\}/g, 'null').replace(/\{%[\s\S]*?%\}/g, '');
const SEED = JSON.stringify({ meters: {
  electricity_main: { meta:{type:'electricity',sub_meter:false,billing_day:1,block_minutes:30},
    channels:{import:{read:'sensor.imp',rate:''},export:{read:'sensor.exp',rate:''}} } }});
js = js.replace('let config   = null;', 'let config = ' + SEED + ';');
js = js.replace('var DATA_SOURCE_MODE = null;', 'var DATA_SOURCE_MODE = "cad";');

// the Device Sensors step as the user filled it
const FIELDS = {
  'wiz-sub-battery-name': 'Battery',
  'wiz-sub-battery-read': 'sensor.battery_grid_import',
  'wiz-sub-battery-soc': 'sensor.battery_soc',
  'wiz-sub-battery-inverter-power': 'sensor.inverter_power',
  'wiz-sub-battery-pv-power': 'sensor.pv_power',
};
const noop = () => {};
function fakeEl(id){ return { value: FIELDS[id] || '', checked:false, disabled:false, innerHTML:'',
  textContent:'', style:{}, dataset:{}, classList:{add:noop,remove:noop,toggle:noop,contains:()=>false},
  addEventListener:noop, removeEventListener:noop, appendChild:noop, removeChild:noop,
  setAttribute:noop, getAttribute:()=>null, focus:noop, querySelector:()=>fakeEl(),
  querySelectorAll:()=>[], closest:()=>null, remove:noop, click:noop, insertAdjacentHTML:noop }; }
const sandbox = { console,
  document:{ getElementById:(id)=>fakeEl(id), querySelector:()=>fakeEl(), querySelectorAll:()=>[],
    addEventListener:noop, createElement:()=>fakeEl(), body:{appendChild:noop} },
  fetch: async()=>({ json: async()=>({ok:true}) }),
  sessionStorage:{ getItem:()=>null, setItem:noop, removeItem:noop },
  alert:noop, setTimeout:noop, clearTimeout:noop, location:{href:'', hash:'', reload:noop},
  addEventListener:noop, removeEventListener:noop, history:{replaceState:noop} };
sandbox.window = sandbox;
const prelude = ['renderWizStep','renderAll','closeWizard','show','showError',
  'attachEntityPickers','wizApplyEntityPickers'].map(n=>`function ${n}(){}`).join('\n')
  + '\nvar apiUrl=function(u){return u;};\n';
const ctx = vm.createContext(sandbox);
try { vm.runInContext(prelude + js, ctx, {filename:'wiz.js'}); }
catch(e){ console.log('LOAD ERROR:', e.message); process.exit(2); }

(async()=>{
  // the page's own renderers draw cards this stub DOM cannot hold; the test reads the config
  vm.runInContext("renderAll = function(){}; closeWizard = function(){};", ctx);
  vm.runInContext("WIZ_STEPS = ['sub_sensors']; wiz.step = 0; wiz.devices = ['battery'];" +
                  "wiz.data = {mainId:'electricity_main', subMeters:{}, mode:'cad'};", ctx);
  vm.runInContext('wizCollectStep();', ctx);
  await vm.runInContext('wizApplyAndClose();', ctx);
  const config = vm.runInContext('config', ctx);
  const id = Object.keys(config.meters).find(k => (config.meters[k].meta||{}).meter_type === 'battery');
  console.log(JSON.stringify(id ? config.meters[id].meta : null));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node not available")
class TheBatterySensors(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(HARNESS)
            path = fh.name
        try:
            res = subprocess.run(["node", path, TEMPLATE], capture_output=True, text=True,
                                 cwd=HERE, timeout=60)
        finally:
            os.unlink(path)
        assert res.returncode == 0, f"harness failed:\n{res.stdout}\n{res.stderr}"
        cls.meta = json.loads(res.stdout.strip().splitlines()[-1])

    def test_the_pv_sensor_is_kept(self):
        self.assertEqual(self.meta.get("pv_power_sensor"), "sensor.pv_power")

    def test_GUARD_the_other_sensors_are_kept(self):
        self.assertEqual(self.meta.get("soc_sensor"), "sensor.battery_soc")
        self.assertEqual(self.meta.get("inverter_power_sensor"), "sensor.inverter_power")
        self.assertEqual(self.meta.get("device"), "Battery")


if __name__ == "__main__":
    unittest.main()

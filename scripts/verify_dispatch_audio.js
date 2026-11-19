/* Measures the real browser generator without an audio device. */
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");
const src = fs.readFileSync(path.join(__dirname, "../static/updates29.js"), "utf8");
const body = src.slice(src.indexOf("const orderTones ="), src.indexOf("function playSound29"));
for (const rate of [44100, 48000]) {
  let samples, started = false;
  const audio = {
    sampleRate: rate, destination: {},
    createBuffer(channels, length, sampleRate) {
      assert.equal(sampleRate, rate);
      samples = new Float32Array(length);
      return {getChannelData() { return samples; }};
    },
    createBufferSource() { return {connect() {}, disconnect() {}, start() { started = true; }}; },
    createGain() { return {gain: {}, connect() {}, disconnect() {}}; },
  };
  vm.runInNewContext(body + "\norderSignal(audio, 1);", {audio});
  assert.ok(started);
  assert.equal(samples.length / rate, 3);
  for (let i = 0; i < 30; i++) {
    const window = samples.subarray(i * rate / 10, (i + 1) * rate / 10);
    const energy = window.reduce((total, x) => total + x * x, 0);
    assert.ok(Math.sqrt(energy / window.length) > 0.5);
  }
  console.log(rate + " Hz: " + samples.length + " samples, 3.000 s");
}

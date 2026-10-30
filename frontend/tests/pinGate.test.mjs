// createPinGate (src/components/pinGate.js). Run with `npm test` (node --test).
import test from 'node:test';
import assert from 'node:assert/strict';

import { createPinGate } from '../src/components/pinGate.js';

const pinError = (reason = 'verify') => Object.assign(new Error('PIN required'), { pinRequired: reason });
const tick = () => new Promise((r) => setTimeout(r, 0));

function harness() {
    const reasons = [];
    const gate = createPinGate((r) => reasons.push(r));
    return { gate, reasons, lastReason: () => reasons[reasons.length - 1] ?? null };
}

// fn that answers from a script: each call takes the next entry (an Error is thrown).
function scripted(...answers) {
    let calls = 0;
    const fn = async () => {
        const answer = answers[calls++];
        if (answer instanceof Error) throw answer;
        return answer;
    };
    return { fn, calls: () => calls };
}

test('no PIN needed: run resolves with the result, no prompt', async () => {
    const { gate, reasons } = harness();
    assert.deepEqual(await gate.run(async () => ({ ok: 1 })), { ok: 1 });
    assert.deepEqual(reasons, []);
});

test('any other error rejects run untouched', async () => {
    const { gate } = harness();
    const boom = new Error('Paystack error: x');
    await assert.rejects(gate.run(async () => { throw boom; }), boom);
});

test('a function that throws synchronously is handled like an async one', async () => {
    const { gate } = harness();
    await assert.rejects(gate.run(() => { throw new Error('sync'); }), /sync/);
});

test('Pay Now after a PIN prompt: the code after run gets the replayed answer', async () => {
    // The bug: run resolved undefined when it parked, the replay's
    // redirect_url was thrown away, and the browser never went to Paystack.
    const { gate, lastReason } = harness();
    const api = scripted(pinError(), { redirect_url: 'https://checkout.paystack.com/abc' });
    let redirectedTo = null;
    const handlePay = async () => {
        const res = await gate.run(api.fn);
        if (res === undefined) return;
        if (res?.redirect_url) redirectedTo = res.redirect_url;
    };

    const paying = handlePay();
    await tick();
    assert.equal(lastReason(), 'verify', 'the prompt is up');
    assert.equal(redirectedTo, null, 'nothing happens while the prompt is up');

    await gate.unlocked();
    await paying;
    assert.equal(api.calls(), 2, 'the call was replayed once');
    assert.equal(lastReason(), null, 'the prompt is gone');
    assert.equal(redirectedTo, 'https://checkout.paystack.com/abc');
});

test('cancelling the prompt resolves run with undefined and replays nothing', async () => {
    const { gate, lastReason } = harness();
    const api = scripted(pinError('setup'), 'never');
    const pending = gate.run(api.fn);
    await tick();
    assert.equal(lastReason(), 'setup');
    gate.cancel();
    assert.equal(await pending, undefined);
    assert.equal(api.calls(), 1);
    assert.equal(lastReason(), null);
});

test('an error from the replay reaches the caller instead of vanishing', async () => {
    const { gate } = harness();
    const api = scripted(pinError(), new Error('Paystack error: Duplicate Transaction Reference'));
    const pending = gate.run(api.fn);
    await tick();
    await gate.unlocked();
    await assert.rejects(pending, /Duplicate Transaction Reference/);
});

test('refused again after the PIN (it locked meanwhile): prompt again, caller keeps waiting', async () => {
    const { gate, reasons } = harness();
    const api = scripted(pinError('verify'), pinError('locked'), 'done');
    const pending = gate.run(api.fn);
    let settled = false;
    pending.then(() => { settled = true; });
    await tick();
    await gate.unlocked();
    await tick();
    assert.equal(reasons.at(-1), 'locked');
    assert.equal(settled, false);
    await gate.unlocked();
    assert.equal(await pending, 'done');
    assert.equal(api.calls(), 3);
});

test('a second gated call while one waits gives up on the first', async () => {
    const { gate } = harness();
    const first = gate.run(scripted(pinError(), 'first').fn);
    await tick();
    const second = gate.run(scripted(pinError(), 'second').fn);
    await tick();
    assert.equal(await first, undefined, 'the first caller is released, not left hanging');
    await gate.unlocked();
    assert.equal(await second, 'second');
});

test('unlocked with nothing parked is harmless', async () => {
    const { gate } = harness();
    await gate.unlocked();
    gate.cancel();
});

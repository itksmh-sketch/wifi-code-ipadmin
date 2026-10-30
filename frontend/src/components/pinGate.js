// The logic behind usePinGate (PinPrompt.jsx), free of React so node can test
// it: `npm test` runs tests/pinGate.test.mjs.
//
// run(fn) calls fn and settles with its outcome. When the API answers that a
// PIN is needed, the call is parked and run's promise stays pending: once the
// PIN clears, unlocked() replays fn and run resolves with the REPLAY's result,
// or rejects with its error; cancel() resolves it with undefined. So the code
// after `await gate.run(...)` always sees the call that actually went through.
// (Before, run resolved undefined as soon as it parked and the replay's result
// was thrown away: Pay Now opened a Paystack checkout nobody was sent to.)
export function createPinGate(setReason) {
    let parked = null;

    const park = (entry, err) => {
        // A second gated call while one waits: the first is given up on.
        if (parked && parked !== entry) parked.resolve(undefined);
        parked = entry;
        setReason(err.pinRequired);
    };

    const run = (fn) => new Promise((resolve, reject) => {
        Promise.resolve().then(fn).then(resolve, (err) => {
            if (err && err.pinRequired) park({ fn, resolve, reject }, err);
            else reject(err);
        });
    });

    const cancel = () => {
        const entry = parked;
        parked = null;
        setReason(null);
        if (entry) entry.resolve(undefined);
    };

    const unlocked = async () => {
        const entry = parked;
        parked = null;
        setReason(null);
        if (!entry) return;
        try {
            entry.resolve(await entry.fn());
        } catch (err) {
            // Refused again (a PIN that locked between the two calls): prompt
            // again, and the caller keeps waiting.
            if (err && err.pinRequired) park(entry, err);
            else entry.reject(err);
        }
    };

    return { run, cancel, unlocked };
}

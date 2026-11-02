// Test-only lifecycle containment. Mongo's detached killer must stay in this
// runner's new process group so even an immediately exiting parent is reaped.
// Non-Node executables retain native options (Playwright kills browser groups).
const cp = require('node:child_process');
const path = require('node:path');
for (const name of ['spawn', 'spawnSync', 'fork', 'execFile', 'execFileSync']) {
  const original = cp[name];
  cp[name] = function (file, args, options, callback) {
    if (!Array.isArray(args)) {
      callback = options;
      options = args;
      args = [];
    }
    if (typeof options === 'function') { callback = options; options = {}; }
    const nodeChild = name === 'fork' || path.resolve(file) === process.execPath;
    const restricted = nodeChild ? { ...options, detached: false } : options;
    return callback === undefined ? original.call(this, file, args, restricted)
      : original.call(this, file, args, restricted, callback);
  };
}

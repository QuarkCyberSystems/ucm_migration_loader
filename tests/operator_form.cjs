const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(
  path.join(
    __dirname,
    "../ucm_migration_loader/ucm_migration_loader/doctype/ucm_migration_run/ucm_migration_run.js"
  ),
  "utf8"
);
const requests = [];
let refresh, route, dialog;
let confirm = false;
vm.runInNewContext(script, {
  __: (s, args = []) => s.replace(/\{(\d+)\}/g, (_, i) => args[i]),
  frappe: {
    ui: {
      Dialog: class {
        constructor(spec) {
          dialog = spec;
        }
        show() {}
        hide() {}
      },
      form: {
        on: (dt, handlers) => {
          assert.equal(dt, "UCM Migration Run");
          refresh = handlers.refresh;
        },
      },
    },
    set_route: (...args) => {
      route = JSON.stringify(args);
    },
    call: async (opts) => {
      requests.push(opts);
      return {
        message: opts.method.endsWith(".options")
          ? [
              {
                doctype: "Person Note",
                selected: "Document API",
                sql_allowed: true,
              },
              { doctype: "Case", selected: "Document API", sql_allowed: false },
            ]
          : {},
      };
    },
    confirm: (_, action) => confirm && action(),
  },
});

let progress;
function buttons(state, report = state === "Ready" ? "{}" : "", fields = {}) {
  const actions = new Map();
  refresh({
    doc: { name: "test-run", state, reconciliation_json: report, ...fields },
    dashboard: {
      show_progress: (...args) => {
        progress = args;
      },
    },
    is_new: () => false,
    add_custom_button: (label, action) => actions.set(label, action),
    reload_doc: () => {},
  });
  return actions;
}

(async () => {
  buttons("Publication ready", "", {
    manifest_json: '{"batches":2160}',
    applied: 2159,
  });
  assert.equal(progress[1], 99);
  assert.match(progress[2], /1 remaining.*activation/);
  buttons("Receiving", "", {
    manifest_json: '{"batches":["a","b","c"]}',
    received: 2,
  });
  assert.match(
    progress[2],
    /0 of 3 batches finished · 3 remaining · 2 received/
  );
  buttons("Publication ready").get("View batches")();
  assert.equal(
    route,
    JSON.stringify([
      "List",
      "UCM Migration Batch",
      { migration_run: "test-run" },
    ])
  );
  assert(buttons("Publication staged").has("Apply hidden batches"));
  assert(buttons("Publication staged").has("Configure write routes"));
  assert(!buttons("Publication applying").has("Configure write routes"));
  assert(!buttons("Publication paused").has("Configure write routes"));
  await buttons("Publication staged").get("Configure write routes")();
  assert.equal(
    requests.pop().method,
    "ucm_migration_loader.write_routes.options"
  );
  assert.deepEqual(Array.from(dialog.fields[0].options), [
    "Document API",
    "Validated bulk SQL",
  ]);
  assert.equal(dialog.fields[1].read_only, true);
  await dialog.primary_action({ route_0: "Validated bulk SQL" });
  const save = requests.pop();
  assert.equal(save.method, "ucm_migration_loader.write_routes.configure");
  assert.deepEqual(
    { ...save.args.routes },
    { "Person Note": "Validated bulk SQL", Case: "Document API" }
  );
  assert(buttons("Publication applying").has("Pause after current batch"));
  assert(buttons("Publication paused").has("Resume from last receipt"));
  assert(buttons("Publication cleaning").has("Resume cleanup"));
  assert(buttons("Publication ready").has("Activate delegation"));
  assert(!buttons("Publication applying").has("Activate delegation"));
  assert(!buttons("Staged").has("Activate delegation"));
  assert(!buttons("Verified").has("Activate delegation"));
  assert(!buttons("Activated").has("Discard run"));
  assert(
    buttons("Activated", '{"shares_held_at_activation":12}').has(
      "Historical share receipts"
    )
  );
  assert(
    buttons("Activated", '{"inbound_share_outcomes":{"Review":3}}').has(
      "Shares requiring review"
    )
  );
  assert(buttons("Ready").has("Download report"));
  await buttons("Paused").get("Resume from last receipt")();
  assert.equal(requests.pop().method, "ucm_migration_loader.api.apply");
  await buttons("Ready").get("Activate delegation")();
  assert.equal(requests.length, 0);
  confirm = true;
  await buttons("Ready").get("Activate delegation")();
  assert.equal(requests[0].args.run, "test-run");
  assert.equal(requests[0].method, "ucm_migration_loader.api.activate");
  console.log("Desk migration commands passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

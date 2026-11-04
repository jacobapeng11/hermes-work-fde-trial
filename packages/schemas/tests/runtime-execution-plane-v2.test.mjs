import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const fixtureDirectory = fileURLToPath(
  new URL("../fixtures/runtime-execution-plane-v1/", import.meta.url),
);

async function loadFixture(name) {
  return JSON.parse(await readFile(`${fixtureDirectory}${name}`, "utf8"));
}

function assertExactKeys(value, expectedKeys) {
  assert.deepEqual(Object.keys(value).sort(), [...expectedKeys].sort());
}

const redirectIdentityKeys = [
  "redirect_id",
  "client_message_id",
  "parent_run_id",
  "input_admission_seq",
  "dispatch_group_id",
  "dispatch_group_position",
  "event_seq",
  "start_after_seq",
];

test("Runtime Edge redirect fixtures carry the complete member identity", async () => {
  const cases = [
    ["web-redirect-pending.json", "run.redirect_pending", [], "ungrouped"],
    ["web-redirect-accepted.json", "run.redirect_accepted", [], "accepted"],
    ["web-redirect-consumed.json", "run.redirect_consumed", [], "accepted"],
    [
      "web-redirect-promoted.json",
      "run.redirect_promoted",
      ["promoted_run_id", "promoted_enqueue_seq"],
      "grouped_unaccepted",
    ],
    [
      "web-redirect-canceled.json",
      "run.redirect_canceled",
      ["cancel_reason"],
      "ungrouped",
    ],
    [
      "web-steer-submission-uncertain.json",
      "run.steer_submission_uncertain",
      ["reason_code"],
      "accepted",
    ],
  ];

  for (const [fileName, type, extraKeys, phase] of cases) {
    const frame = await loadFixture(fileName);
    assertExactKeys(frame, [
      "schema_version",
      "type",
      ...redirectIdentityKeys,
      ...extraKeys,
    ]);
    assert.equal(frame.schema_version, 1);
    assert.equal(frame.type, type);
    assert.ok(frame.redirect_id.length > 0);
    assert.ok(frame.client_message_id.length > 0);
    assert.ok(frame.parent_run_id.length > 0);
    assert.ok(Number.isSafeInteger(frame.input_admission_seq));
    assert.ok(frame.input_admission_seq > 0);
    assert.ok(Number.isSafeInteger(frame.event_seq));
    assert.ok(frame.event_seq > 0);

    if (phase === "ungrouped") {
      assert.equal(frame.dispatch_group_id, null);
      assert.equal(frame.dispatch_group_position, null);
      assert.equal(frame.start_after_seq, null);
    } else {
      assert.ok(frame.dispatch_group_id.length > 0);
      assert.ok(Number.isSafeInteger(frame.dispatch_group_position));
      assert.ok(frame.dispatch_group_position >= 0);
      if (phase === "accepted") {
        assert.ok(Number.isSafeInteger(frame.start_after_seq));
        assert.ok(frame.start_after_seq > 0);
      } else {
        assert.equal(frame.start_after_seq, null);
      }
    }
  }

  const promoted = await loadFixture("web-redirect-promoted.json");
  assert.equal(promoted.promoted_enqueue_seq, promoted.input_admission_seq);

  const canceled = await loadFixture("web-redirect-canceled.json");
  assert.equal(canceled.cancel_reason, "PARENT_RUN_CANCELED");

  const uncertain = await loadFixture(
    "web-steer-submission-uncertain.json",
  );
  assert.equal(uncertain.reason_code, "REDIRECT_DELIVERY_UNPROVEN");
});

test("parent Run steer events repeat the exact redirect member identity", async () => {
  const cases = [
    ["web-run-event-steer-accepted.json", "chat.steer.accepted"],
    ["web-run-event-steer-consumed.json", "chat.steer.consumed"],
  ];

  for (const [fileName, eventType] of cases) {
    const event = await loadFixture(fileName);
    assertExactKeys(event, [
      "schema_version",
      "type",
      "run_id",
      "seq",
      "event_type",
      "emitted_at",
      "data",
    ]);
    assertExactKeys(event.data, redirectIdentityKeys);
    assert.equal(event.schema_version, 1);
    assert.equal(event.type, "run.event");
    assert.equal(event.event_type, eventType);
    assert.equal(event.run_id, event.data.parent_run_id);
    assert.equal(event.seq, event.data.event_seq);
    assert.ok(event.data.dispatch_group_id.length > 0);
    assert.ok(Number.isSafeInteger(event.data.dispatch_group_position));
    assert.ok(Number.isSafeInteger(event.data.start_after_seq));
  }
});

test("Fast Lane v2 submit, resume, and ACK fixtures use delivery identity", async () => {
  const submit = await loadFixture("channel-fast-lane-v2-submit.json");
  assertExactKeys(submit, [
    "schema_version",
    "channel_fast_lane_schema_version",
    "type",
    "request_id",
    "gateway_instance_id",
    "channel_lease_epoch",
    "delivery_id",
    "client_message_id",
    "envelope",
  ]);
  assert.equal(submit.schema_version, 1);
  assert.equal(submit.channel_fast_lane_schema_version, 2);
  assert.equal(submit.type, "channel.turn.submit");
  assert.equal(submit.client_message_id, submit.envelope.payload.client_message_id);
  const v1Submit = await loadFixture("channel-fast-lane-submit.json");
  assert.deepEqual(submit.envelope.payload, v1Submit.envelope.payload);
  assert.equal(submit.envelope.payload_digest, v1Submit.envelope.payload_digest);

  const resume = await loadFixture("channel-fast-lane-v2-resume.json");
  assertExactKeys(resume, [
    "schema_version",
    "channel_fast_lane_schema_version",
    "type",
    "request_id",
    "gateway_instance_id",
    "channel_lease_epoch",
    "delivery_id",
    "last_disposition_seq",
    "last_event_seq",
  ]);
  assert.equal(resume.channel_fast_lane_schema_version, 2);
  assert.equal(resume.type, "channel.turn.resume");

  const dispositionAck = await loadFixture(
    "channel-fast-lane-v2-disposition-ack.json",
  );
  assertExactKeys(dispositionAck, [
    "schema_version",
    "channel_fast_lane_schema_version",
    "type",
    "request_id",
    "gateway_instance_id",
    "channel_lease_epoch",
    "delivery_id",
    "disposition_seq",
  ]);
  assert.equal(dispositionAck.channel_fast_lane_schema_version, 2);
  assert.equal(dispositionAck.type, "channel.turn.disposition.ack");

  const providerAck = await loadFixture("channel-delivery-v2-ack.json");
  assertExactKeys(providerAck, [
    "schema_version",
    "channel_fast_lane_schema_version",
    "type",
    "request_id",
    "gateway_instance_id",
    "channel_lease_epoch",
    "owner_delivery_id",
    "canonical_run_id",
    "highest_contiguous_seq",
    "provider_request_id",
    "provider_message_id",
    "projection_version",
    "confirmed_at",
  ]);
  assert.equal(providerAck.channel_fast_lane_schema_version, 2);
  assert.equal(providerAck.type, "channel.delivery.ack");
  assert.equal("run_id" in providerAck, false);
});

test("Fast Lane v2 disposition fixtures close the state and identity unions", async () => {
  const fixtureByState = new Map([
    ["pending", "channel-fast-lane-v2-disposition-pending.json"],
    [
      "ordinary_run_accepted",
      "channel-fast-lane-v2-disposition-ordinary-run-accepted.json",
    ],
    [
      "redirect_accepted",
      "channel-fast-lane-v2-disposition-redirect-accepted.json",
    ],
    [
      "redirect_consumed",
      "channel-fast-lane-v2-disposition-redirect-consumed.json",
    ],
    ["promoted", "channel-fast-lane-v2-disposition-promoted.json"],
    ["uncertain", "channel-fast-lane-v2-disposition-uncertain.json"],
    ["canceled", "channel-fast-lane-v2-disposition-canceled.json"],
  ]);
  const dispositionKeys = [
    "schema_version",
    "channel_fast_lane_schema_version",
    "type",
    "delivery_id",
    "client_message_id",
    "disposition_seq",
    "state",
    "canonical_run_id",
    "parent_run_id",
    "start_after_seq",
    "promoted_run_id",
  ];

  for (const [state, fileName] of fixtureByState) {
    const frame = await loadFixture(fileName);
    assertExactKeys(frame, dispositionKeys);
    assert.equal(frame.schema_version, 1);
    assert.equal(frame.channel_fast_lane_schema_version, 2);
    assert.equal(frame.type, "channel.turn.disposition");
    assert.equal(frame.state, state);
    assert.ok(frame.delivery_id.length > 0);
    assert.ok(frame.client_message_id.length > 0);
    assert.ok(Number.isSafeInteger(frame.disposition_seq));
    assert.ok(frame.disposition_seq > 0);

    switch (state) {
      case "pending":
      case "canceled":
        assert.equal(frame.canonical_run_id, frame.parent_run_id);
        assert.equal(frame.start_after_seq, null);
        assert.equal(frame.promoted_run_id, null);
        break;
      case "ordinary_run_accepted":
        assert.equal(frame.parent_run_id, null);
        assert.equal(frame.start_after_seq, 0);
        assert.equal(frame.promoted_run_id, null);
        break;
      case "redirect_accepted":
      case "redirect_consumed":
      case "uncertain":
        assert.equal(frame.canonical_run_id, frame.parent_run_id);
        assert.ok(frame.start_after_seq > 0);
        assert.equal(frame.promoted_run_id, null);
        break;
      case "promoted":
        assert.equal(frame.canonical_run_id, frame.promoted_run_id);
        assert.equal(frame.start_after_seq, 0);
        assert.notEqual(frame.parent_run_id, frame.promoted_run_id);
        break;
      default:
        assert.fail(`unexpected disposition state: ${state}`);
    }
  }
});

test("legacy v1 fixtures cannot masquerade as v2 deliveries", async () => {
  const v1Submit = await loadFixture("channel-fast-lane-submit.json");
  assert.equal("channel_fast_lane_schema_version" in v1Submit, false);
  assert.equal("delivery_id" in v1Submit, false);

  const v1ProviderAck = await loadFixture("channel-delivery-ack.json");
  assert.equal("channel_fast_lane_schema_version" in v1ProviderAck, false);
  assert.equal("delivery_id" in v1ProviderAck, false);
  assert.equal("owner_delivery_id" in v1ProviderAck, false);
  assert.ok(v1ProviderAck.run_id.length > 0);
});

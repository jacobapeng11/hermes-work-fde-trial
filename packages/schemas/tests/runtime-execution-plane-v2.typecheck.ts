import type {
  ChannelFastLaneV1DeliveryAck,
  ChannelFastLaneV1SubmitFrame,
  ChannelFastLaneV2DeliveryAck,
  ChannelFastLaneV2Disposition,
  ChannelFastLaneV2SubmitFrame,
  RuntimeEdgeRedirectDisposition,
  RuntimeEdgeServerMessage,
  RuntimeEdgeSteerAcceptedEvent,
  RuntimeEdgeSteerConsumedEvent,
  RuntimeRedirectMemberIdentity,
} from "../src/index";

type Equal<Left, Right> =
  (<Value>() => Value extends Left ? 1 : 2) extends
  (<Value>() => Value extends Right ? 1 : 2)
    ? true
    : false;

type Assert<Value extends true> = Value;

type RedirectIdentityFields = Assert<
  Equal<
    keyof RuntimeRedirectMemberIdentity,
    | "redirect_id"
    | "client_message_id"
    | "parent_run_id"
    | "input_admission_seq"
    | "dispatch_group_id"
    | "dispatch_group_position"
    | "event_seq"
    | "start_after_seq"
  >
>;

type RedirectFrameTypes = Assert<
  Equal<
    RuntimeEdgeRedirectDisposition["type"],
    | "run.redirect_pending"
    | "run.redirect_accepted"
    | "run.redirect_consumed"
    | "run.redirect_promoted"
    | "run.redirect_canceled"
    | "run.steer_submission_uncertain"
  >
>;

type RedirectFramesAreServerMessages = Assert<
  RuntimeEdgeRedirectDisposition extends RuntimeEdgeServerMessage
    ? true
    : false
>;

type ParentRunSteerEventTypes = Assert<
  Equal<
    | RuntimeEdgeSteerAcceptedEvent["event_type"]
    | RuntimeEdgeSteerConsumedEvent["event_type"],
    "chat.steer.accepted" | "chat.steer.consumed"
  >
>;

type ParentRunSteerAcceptedIsServerMessage = Assert<
  RuntimeEdgeSteerAcceptedEvent extends RuntimeEdgeServerMessage
    ? true
    : false
>;

type ParentRunSteerConsumedIsServerMessage = Assert<
  RuntimeEdgeSteerConsumedEvent extends RuntimeEdgeServerMessage
    ? true
    : false
>;

type FastLaneV2DispositionStates = Assert<
  Equal<
    ChannelFastLaneV2Disposition["state"],
    | "pending"
    | "ordinary_run_accepted"
    | "redirect_accepted"
    | "redirect_consumed"
    | "promoted"
    | "uncertain"
    | "canceled"
  >
>;

type FastLaneV2DispositionFields = Assert<
  Equal<
    keyof ChannelFastLaneV2Disposition,
    | "schema_version"
    | "channel_fast_lane_schema_version"
    | "type"
    | "delivery_id"
    | "client_message_id"
    | "disposition_seq"
    | "state"
    | "canonical_run_id"
    | "parent_run_id"
    | "start_after_seq"
    | "promoted_run_id"
  >
>;

type FastLaneV2CarriesDeliveryIdentity = Assert<
  ChannelFastLaneV2SubmitFrame extends {
    channel_fast_lane_schema_version: 2;
    delivery_id: string;
    client_message_id: string;
  }
    ? true
    : false
>;

// V1 remains available for draining, but the explicit `never` fields keep a
// structurally typed v2 submit or provider ACK from entering the v1 path.
type V1RejectsV2Submit = Assert<
  ChannelFastLaneV2SubmitFrame extends ChannelFastLaneV1SubmitFrame
    ? false
    : true
>;

type V1SubmitHasNoV2Marker = Assert<
  Equal<
    Extract<
      ChannelFastLaneV1SubmitFrame,
      { channel_fast_lane_schema_version: 2 }
    >,
    never
  >
>;

type V1RejectsV2DeliveryAck = Assert<
  ChannelFastLaneV2DeliveryAck extends ChannelFastLaneV1DeliveryAck
    ? false
    : true
>;

type V1DeliveryAckHasNoV2Marker = Assert<
  Equal<
    Extract<
      ChannelFastLaneV1DeliveryAck,
      { channel_fast_lane_schema_version: 2 }
    >,
    never
  >
>;

export type RuntimeExecutionPlaneV2ContractAssertions =
  | RedirectIdentityFields
  | RedirectFrameTypes
  | RedirectFramesAreServerMessages
  | ParentRunSteerEventTypes
  | ParentRunSteerAcceptedIsServerMessage
  | ParentRunSteerConsumedIsServerMessage
  | FastLaneV2DispositionStates
  | FastLaneV2DispositionFields
  | FastLaneV2CarriesDeliveryIdentity
  | V1RejectsV2Submit
  | V1RejectsV2DeliveryAck
  | V1SubmitHasNoV2Marker
  | V1DeliveryAckHasNoV2Marker;

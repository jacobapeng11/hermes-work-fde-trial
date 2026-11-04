import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { randomUUID } from "node:crypto";

import AiBot from "@wecom/aibot-node-sdk";


const clients = new Map();
const tempRoot = path.resolve(
  process.env.IM_BRIDGE_TEMP_ROOT || path.join("/tmp", "hermes-im-gateway")
);
const allowedMediaTypes = new Set(["file", "image", "voice", "video"]);

await fs.mkdir(tempRoot, { recursive: true, mode: 0o700 });
await fs.chmod(tempRoot, 0o700);


function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}


function safeError(error) {
  return {
    code: String(error?.code || error?.name || "WECOM_SDK_ERROR"),
    message: "WeCom SDK operation failed",
  };
}


const quietLogger = {
  debug() {},
  info() {},
  warn() {},
  error() {},
};


function requireClient(command) {
  const channelId = String(command.channel_id);
  const entry = clients.get(channelId);
  if (!entry) {
    throw Object.assign(new Error("channel is not connected"), {
      code: "CHANNEL_NOT_CONNECTED",
    });
  }
  if (
    command.generation !== undefined
    && Number(command.generation) !== entry.generation
  ) {
    throw Object.assign(new Error("stale channel generation"), {
      code: "STALE_CHANNEL_GENERATION",
    });
  }
  if (
    command.lease_epoch !== undefined
    && Number(command.lease_epoch) !== entry.leaseEpoch
  ) {
    throw Object.assign(new Error("stale channel lease"), {
      code: "STALE_CHANNEL_LEASE",
    });
  }
  return entry;
}


function frameFrom(command) {
  const reqId = String(command.provider_request_id || "").trim();
  if (!reqId) {
    throw Object.assign(new Error("provider request id is required"), {
      code: "MISSING_PROVIDER_REQUEST_ID",
    });
  }
  return { headers: { req_id: reqId } };
}


function assertScopedPath(candidate) {
  const resolved = path.resolve(String(candidate || ""));
  const relative = path.relative(tempRoot, resolved);
  if (
    !resolved
    || relative.startsWith("..")
    || path.isAbsolute(relative)
    || relative === ""
  ) {
    throw Object.assign(new Error("path escapes bridge temp root"), {
      code: "INVALID_MEDIA_PATH",
    });
  }
  return resolved;
}


function sanitizeFilename(value, fallback) {
  const base = path.basename(String(value || fallback))
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .slice(0, 180);
  return base || fallback;
}


function wireClient(command) {
  const channelId = String(command.channel_id);
  const generation = Number(command.generation);
  const leaseEpoch = Number(command.lease_epoch);
  const client = new AiBot.WSClient({
    botId: String(command.bot_id),
    secret: String(command.secret),
    wsUrl: command.ws_url ? String(command.ws_url) : undefined,
    maxReconnectAttempts: -1,
    maxAuthFailureAttempts: 5,
    logger: quietLogger,
  });
  const context = () => ({
    channel_id: Number(command.channel_id),
    generation,
    lease_epoch: leaseEpoch,
  });
  client.on("connected", () => emit({
    type: "connection.connected",
    ...context(),
  }));
  client.on("authenticated", () => emit({
    type: "connection.authenticated",
    ...context(),
  }));
  client.on("reconnecting", (attempt) => emit({
    type: "connection.reconnecting",
    attempt: Number(attempt),
    ...context(),
  }));
  client.on("disconnected", () => emit({
    type: "connection.disconnected",
    ...context(),
  }));
  client.on("error", (error) => emit({
    type: "connection.error",
    error: safeError(error),
    ...context(),
  }));
  client.on("message", (frame) => emit({
    type: "message",
    frame: {
      headers: { req_id: String(frame?.headers?.req_id || "") },
      body: frame?.body || {},
    },
    ...context(),
  }));
  client.on("event", (frame) => emit({
    type: "event",
    frame: {
      headers: { req_id: String(frame?.headers?.req_id || "") },
      body: frame?.body || {},
    },
    ...context(),
  }));
  client.connect();
  clients.set(channelId, {
    client,
    generation,
    leaseEpoch,
  });
}


async function handle(command) {
  switch (command.op) {
    case "ping":
      return {
        pong: true,
        sdk: "@wecom/aibot-node-sdk",
        sdk_version: "1.0.7",
        capabilities: {
          inbound_messages: [
            "text",
            "image",
            "mixed",
            "voice",
            "file",
            "video",
          ],
          quote_messages: ["text", "image", "mixed", "voice", "file"],
          events: [
            "enter_chat",
            "template_card_event",
            "feedback_event",
            "disconnected_event",
          ],
          outbound: [
            "generic_reply",
            "stream",
            "stream_with_template_card",
            "template_card",
            "update_template_card",
            "welcome",
            "active_markdown",
            "active_template_card",
            "file",
            "image",
            "voice",
            "video",
          ],
        },
      };
    case "connect": {
      const channelId = String(command.channel_id);
      const old = clients.get(channelId);
      if (old) {
        old.client.disconnect();
        clients.delete(channelId);
      }
      wireClient(command);
      return { connected: true };
    }
    case "disconnect": {
      const channelId = String(command.channel_id);
      const old = clients.get(channelId);
      if (old) {
        old.client.disconnect();
        clients.delete(channelId);
      }
      return { disconnected: true };
    }
    case "connection_status": {
      const entry = requireClient(command);
      return { connected: Boolean(entry.client.isConnected) };
    }
    case "reply": {
      const entry = requireClient(command);
      await entry.client.reply(
        frameFrom(command),
        command.body,
        command.cmd ? String(command.cmd) : undefined
      );
      return { delivered: true };
    }
    case "submit_stream_state": {
      const entry = requireClient(command);
      let immediateError;
      const providerAck = entry.client.replyStream(
        frameFrom(command),
        String(command.stream_id),
        String(command.content || ""),
        false
      );
      void providerAck.catch((error) => {
        immediateError ??= error;
      });
      // The pinned SDK enqueues synchronously. Yield one microtask so a
      // synchronous local send/queue rejection is returned to Python, while
      // keeping the provider ACK outside the Runtime startup path.
      await Promise.resolve();
      if (immediateError !== undefined) {
        throw immediateError;
      }
      return { submitted: true };
    }
    case "reply_stream": {
      const entry = requireClient(command);
      const method = command.non_blocking && !command.finish
        ? "replyStreamNonBlocking"
        : "replyStream";
      const result = await entry.client[method](
        frameFrom(command),
        String(command.stream_id),
        String(command.content || ""),
        Boolean(command.finish),
        Array.isArray(command.msg_item) ? command.msg_item : undefined,
        command.feedback && typeof command.feedback === "object"
          ? command.feedback
          : undefined
      );
      return { delivered: result !== "skipped", skipped: result === "skipped" };
    }
    case "reply_stream_with_card": {
      const entry = requireClient(command);
      await entry.client.replyStreamWithCard(
        frameFrom(command),
        String(command.stream_id),
        String(command.content || ""),
        Boolean(command.finish),
        command.options && typeof command.options === "object"
          ? command.options
          : undefined
      );
      return { delivered: true };
    }
    case "reply_welcome": {
      const entry = requireClient(command);
      await entry.client.replyWelcome(frameFrom(command), command.body);
      return { delivered: true };
    }
    case "reply_template_card": {
      const entry = requireClient(command);
      await entry.client.replyTemplateCard(
        frameFrom(command),
        command.template_card,
        command.feedback
      );
      return { delivered: true };
    }
    case "update_template_card": {
      const entry = requireClient(command);
      await entry.client.updateTemplateCard(
        frameFrom(command),
        command.template_card,
        command.userids
      );
      return { delivered: true };
    }
    case "send_markdown": {
      const entry = requireClient(command);
      await entry.client.sendMessage(String(command.chat_id), {
        msgtype: "markdown",
        markdown: { content: String(command.content || "") },
      });
      return { delivered: true };
    }
    case "send_message": {
      const entry = requireClient(command);
      await entry.client.sendMessage(
        String(command.chat_id),
        command.body
      );
      return { delivered: true };
    }
    case "download_media": {
      const entry = requireClient(command);
      const url = new URL(String(command.url));
      if (
        url.protocol !== "https:"
        || url.username
        || url.password
      ) {
        throw Object.assign(new Error("unsupported media URL"), {
          code: "INVALID_MEDIA_URL",
        });
      }
      const downloaded = await entry.client.downloadFile(
        url.toString(),
        command.aes_key ? String(command.aes_key) : undefined
      );
      const fallback = `wecom-${Date.now()}.bin`;
      const filename = sanitizeFilename(
        command.filename || downloaded.filename,
        fallback
      );
      const target = assertScopedPath(
        path.join(
          tempRoot,
          `${Date.now()}-${randomUUID()}-${filename}`
        )
      );
      await fs.writeFile(target, downloaded.buffer, { mode: 0o600 });
      return {
        path: target,
        filename,
        size_bytes: downloaded.buffer.length,
      };
    }
    case "upload_reply_media":
    case "upload_send_media": {
      const entry = requireClient(command);
      const mediaType = String(command.media_type);
      if (!allowedMediaTypes.has(mediaType)) {
        throw Object.assign(new Error("unsupported media type"), {
          code: "UNSUPPORTED_MEDIA_TYPE",
        });
      }
      const source = assertScopedPath(command.path);
      const stat = await fs.stat(source);
      if (!stat.isFile() || stat.size > 50 * 1024 * 1024) {
        throw Object.assign(new Error("media file is invalid or too large"), {
          code: "MEDIA_TOO_LARGE",
        });
      }
      const buffer = await fs.readFile(source);
      const uploaded = await entry.client.uploadMedia(buffer, {
        type: mediaType,
        filename: sanitizeFilename(command.filename, path.basename(source)),
      });
      if (command.op === "upload_reply_media") {
        await entry.client.replyMedia(
          frameFrom(command),
          mediaType,
          uploaded.media_id,
          command.video_options
        );
      } else {
        await entry.client.sendMediaMessage(
          String(command.chat_id),
          mediaType,
          uploaded.media_id,
          command.video_options
        );
      }
      return {
        delivered: true,
        media_id: uploaded.media_id,
        media_type: uploaded.type,
      };
    }
    case "unlink_temp": {
      const target = assertScopedPath(command.path);
      await fs.unlink(target).catch(() => {});
      return { deleted: true };
    }
    case "shutdown":
      for (const entry of clients.values()) {
        entry.client.disconnect();
      }
      clients.clear();
      return { shutdown: true };
    default:
      throw Object.assign(new Error("unknown bridge operation"), {
        code: "UNKNOWN_OPERATION",
      });
  }
}


const input = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

input.on("line", async (line) => {
  let command;
  try {
    command = JSON.parse(line);
  } catch {
    emit({
      type: "response",
      id: null,
      ok: false,
      error: { code: "INVALID_JSON", message: "Invalid JSON command" },
    });
    return;
  }
  try {
    const result = await handle(command);
    emit({ type: "response", id: command.id ?? null, ok: true, result });
    if (command.op === "shutdown") {
      input.close();
      process.exitCode = 0;
    }
  } catch (error) {
    emit({
      type: "response",
      id: command.id ?? null,
      ok: false,
      error: safeError(error),
    });
  }
});

input.on("close", () => {
  for (const entry of clients.values()) {
    entry.client.disconnect();
  }
  clients.clear();
});

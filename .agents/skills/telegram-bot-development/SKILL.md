---
name: telegram-bot-development
description: Build, modify, debug, and deploy Telegram bots using the Bot API and a suitable framework. Use for requests involving bot commands, updates, webhooks, polling, keyboards, payments, or bot integrations; not ordinary Telegram client usage.
---

# Telegram bot development

Create or change Telegram bots in the context of the existing project. Apply the general `programming` skill when available for broader coding tasks.

## Project decisions

- Inspect the repository and its instructions first. Keep its language, framework, and established deployment model. For a new bot, choose a maintained library that fits the requested language and runtime.
- Determine whether the project uses long polling or webhooks and preserve that choice unless asked to change it. Webhooks need HTTPS and safe retry/duplicate handling; polling should have only one active consumer per token.
- Keep bot tokens and other secrets in environment configuration or the project's secret store. Add an `.env.example` with placeholder names only when useful; never put real credentials in source, logs, or examples.
- Treat messages, callback data, and external API responses as untrusted. Validate callback payloads and authorize privileged actions server-side; buttons and hidden commands are not access control.
- Escape user-controlled text when interpolating it into Markdown or HTML. Respect message limits and rate limits, answer callback queries promptly, and keep slow work from blocking update handling.
- For payments or other consequential operations, verify required fields and server-side confirmations; never treat a client-provided callback as proof of payment.

## Bot API work

- Use the official Telegram Bot API documentation as the source of truth for current methods, fields, update behavior, and limits: https://core.telegram.org/bots/api
- Check the installed framework version and its official documentation before using version-specific APIs. Do not invent method names or assume examples match the installed version.
- Request only the update types and permissions the bot needs. Handle malformed or unsupported updates without crashing the process.

## Delivery and verification

- Keep bot logic separate from transport and configuration when that fits the existing codebase.
- Do not send real messages, register webhooks, alter bot settings, or deploy unless the user authorized that external action.
- When verification is requested, use available project checks and mock Telegram/network interactions where practical. Routine local checks should not require a real token; state clearly if a live integration remains unverified.

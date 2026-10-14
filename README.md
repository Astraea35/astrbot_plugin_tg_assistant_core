# Telegram NAS Bridge

Telegram polling, download tasks, message editing and media delivery stay in
`tg-downloader` on the NAS. This plugin registers the `telegram_nas` platform in
AstrBot; plugins receive normal Telegram private-message events, including
replies and media. Scheduled plugin messages return through the same transport.

Create a separate AstrBot profile for the bridge platform and select the personal
plugins explicitly. Keep QQ group-management plugins out of that profile.

The platform requires `nas_url`, `public_url`, `nas_input_share`,
`allowed_user_ids` and a random `shared_secret` of at least 32 characters.
Configure the same secret and AstrBot URL in the NAS `astrbot_bridge` plugin.
Never commit either live configuration or an AstrBot API key.

Both endpoints authenticate every request. Only whitelisted private Telegram
chats are accepted. Local input files must exist under `nas_input_share`.
Generated media are copied to a temporary outbox and exposed by opaque IDs
for 15 minutes; directory paths are never accepted by the file endpoint.

Replies update one Telegram message during a request. Text longer than the
Telegram limit is split into additional pages. Real images, generated artwork,
GIFs, videos and files remain media. Proactive subscriptions create new messages.

AstrBot must remain running for its plugins; the NAS downloader remains usable
when AstrBot is offline. The bridge uses direct LAN HTTP and does not inherit
the Windows system proxy.

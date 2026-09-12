# Finding the `daoapp://…` redirect URL

DAO finishes sign-in by redirecting the browser to an address that begins
with `daoapp://`. On a desktop browser the DAO app is normally not installed,
so the last navigation may fail or remain on the address bar. That is expected:
Home Assistant needs that complete failed URL to finish the secure PKCE login.

1. Open the link shown by the DAO Home Assistant configuration flow in a
   desktop browser and sign in normally.
2. When the browser tries to open `daoapp://auth-callback`, copy the complete
   address from the address bar, including `?code=…&state=…`.
3. Paste it unchanged into the Home Assistant form.

If the browser hides the failed redirect, open Developer Tools before signing
in (`F12`, or `Cmd+Option+I` on macOS), select **Network**, enable **Preserve
log** / **Persist Logs**, complete sign-in and filter for `daoapp`. Copy the
full request URL from **Headers**.

Use a desktop browser: mobile browsers generally hand this link to the DAO app
and do not expose a network inspector. The authorization code is single-use
and short-lived; if Home Assistant rejects it, repeat the login and copy the
new URL. Never share the redirect URL, authorization code or token in an issue.

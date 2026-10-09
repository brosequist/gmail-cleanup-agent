# Google OAuth setup

To use this tool, you need to authorize it against your own Google account.
You'll create a Google Cloud project, enable the Gmail API, create OAuth
credentials, and download a `credentials.json` file. This is a one-time
setup that takes ~5 minutes.

> The OAuth app you create only authorizes _your_ account against your own
> tool. It never needs Google's verification for personal single-user use,
> but read step 6: an app left in "Testing" stops working after 7 days.

## 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Top-left, click the project dropdown → **New Project**.
3. Name it something like "gmail-cleanup-agent". Click **Create**.

## 2. Enable the Gmail API

1. In the new project, go to
   [APIs & Services → Library](https://console.cloud.google.com/apis/library).
2. Search **Gmail API**, click it, click **Enable**.

## 3. Configure the OAuth consent screen

1. Left nav: **APIs & Services → OAuth consent screen** (newer consoles
   call it **Google Auth Platform**, with *Branding*, *Audience* and
   *Data access* tabs; the fields are the same).
2. **User type**: External (this is fine for a single-user personal app).
3. App information:
   - App name: `gmail-cleanup-agent`
   - User support email: your email
   - Developer contact: your email
4. **Save and continue**.
5. **Scopes** screen: click **Add or Remove Scopes**, find and add:
   - `https://www.googleapis.com/auth/gmail.modify`
   (this lets the tool read messages, apply labels, and trash messages —
   but **not** permanently delete or read your Drive/Calendar etc.)
6. Save and continue through the rest.
7. **Test users**: add your own Gmail address. (Required while the app is
   in "Testing" mode; see step 6 for why you will want to leave it.)

## 4. Create OAuth client credentials

1. Left nav: **APIs & Services → Credentials**.
2. **Create Credentials → OAuth client ID**.
3. Application type: **Desktop app**.
4. Name: `gmail-cleanup-agent`.
5. Click **Create**.
6. Click **Download JSON** on the resulting credential.
7. Save the file as `credentials.json` in your **config directory**:
   `./config/` relative to where you run the tool (a repo checkout, or
   any directory for a PyPI install), or wherever
   `GMAIL_CLEANUP_CONFIG_DIR` points (`/config` inside the Docker
   image, i.e. the host directory you mount there):

```bash
mkdir -p config
mv ~/Downloads/client_secret_*.json config/credentials.json
```

## 5. Authorize

```bash
gmail-cleanup auth          # or: python -m gmail_cleanup auth
```

This opens a browser to Google's consent screen. Approve the requested
scopes; the tool writes a refresh token to `token.json` in the same
config directory and prints the exact path. You won't need to
re-authorize unless you revoke access in
[your Google account settings](https://myaccount.google.com/permissions),
or the app is still in Testing (next step).

## 6. Move the app to "In production" (or re-authorize weekly)

While the consent screen's publishing status is **Testing**, Google
expires the refresh token **7 days** after you consent, and refreshing
does not extend it. The tool then fails with `invalid_grant` until you
run `auth` again. For anything longer than a one-week cleanup:

1. **OAuth consent screen** (or **Google Auth Platform → Audience**) →
   **Publish app** → confirm.
2. Leave it **unverified**. `gmail.modify` is a restricted scope, so
   verification would mean a paid security assessment; an unverified
   app in production is fine for your own account (it is capped at
   100 users and shows an "unverified app" warning once, at consent:
   **Advanced → Go to … (unsafe)**).
3. Run `auth` again. Publishing does not revive a token issued while
   the app was in Testing.

## Running `auth` for Docker or a headless machine

`auth` starts a temporary web server on a random local port and opens a
browser that Google redirects back to it, so the browser and the tool
must be on the **same machine**. That does not work inside a container
or over SSH. Instead, authorize once on a machine with a browser and
copy the token across:

```bash
# on your desktop
pipx install gmail-llm-cleanup
mkdir -p config && cp /path/to/credentials.json config/
gmail-cleanup auth                    # writes config/token.json

# then put both files in the directory you mount as /config
scp config/credentials.json config/token.json server:~/gmail-cleanup/config/
```

The token refreshes itself from then on; the container only needs to
read and rewrite `token.json`, so mount the config directory writable.

## Revoking access later

To remove the tool's access:

1. Go to https://myaccount.google.com/permissions
2. Find `gmail-cleanup-agent` in the list and click **Remove Access**.

You can also delete the OAuth client and the Cloud project entirely from
the Cloud Console once you're done with the tool.

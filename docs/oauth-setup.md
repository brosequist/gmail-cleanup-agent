# Google OAuth setup

To use this tool, you need to authorize it against your own Google account.
You'll create a Google Cloud project, enable the Gmail API, create OAuth
credentials, and download a `credentials.json` file. This is a one-time
setup that takes ~5 minutes.

> The OAuth app you create only authorizes _your_ account against your own
> tool — there's nothing to publish or get verified by Google for personal
> single-user use.

## 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Top-left, click the project dropdown → **New Project**.
3. Name it something like "gmail-cleanup-agent". Click **Create**.

## 2. Enable the Gmail API

1. In the new project, go to
   [APIs & Services → Library](https://console.cloud.google.com/apis/library).
2. Search **Gmail API**, click it, click **Enable**.

## 3. Configure the OAuth consent screen

1. Left nav: **APIs & Services → OAuth consent screen**.
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
   in "Testing" mode. You don't need to publish the app.)

## 4. Create OAuth client credentials

1. Left nav: **APIs & Services → Credentials**.
2. **Create Credentials → OAuth client ID**.
3. Application type: **Desktop app**.
4. Name: `gmail-cleanup-agent`.
5. Click **Create**.
6. Click **Download JSON** on the resulting credential.
7. Save the file as `config/credentials.json` in your local checkout of
   this repo:

```bash
mv ~/Downloads/client_secret_*.json config/credentials.json
```

## 5. Authorize

```bash
python -m gmail_cleanup auth
```

This opens a browser to Google's consent screen. Approve the requested
scopes; the tool stores a refresh token in `config/token.json` and you
won't need to re-authorize unless you revoke access in
[your Google account settings](https://myaccount.google.com/permissions).

## Revoking access later

To remove the tool's access:

1. Go to https://myaccount.google.com/permissions
2. Find `gmail-cleanup-agent` in the list and click **Remove Access**.

You can also delete the OAuth client and the Cloud project entirely from
the Cloud Console once you're done with the tool.

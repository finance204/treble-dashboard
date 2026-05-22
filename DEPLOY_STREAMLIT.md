# Deploy Treble Dashboard

Use this checklist to publish the dashboard with Google login restricted to Treble.

## 1. Publish the code

Create a private GitHub repository and upload only these project files:

- `app.py`
- `requirements.txt`
- `.gitignore`
- `.streamlit/secrets.example.toml`
- `DEPLOY_STREAMLIT.md`

Do not upload:

- `credentials/`
- `.env`
- `.streamlit/secrets.toml`
- any `.csv` files
- `.python_packages/`

## 2. Create the Streamlit app

In Streamlit Cloud:

1. Create a new app from the private GitHub repository.
2. Set the main file path to `app.py`.
3. Deploy once to get the public app URL.

The first deploy can fail until secrets are added. That is expected.

## 3. Create Google OAuth credentials

In Google Cloud Console, in the same project:

1. Go to APIs & Services > Credentials.
2. Create OAuth client ID.
3. Select Web application.
4. Add this Authorized redirect URI:

```text
https://YOUR_STREAMLIT_APP_URL/oauth2callback
```

5. Copy the Client ID and Client Secret.

## 4. Add Streamlit secrets

In Streamlit Cloud > App settings > Secrets, add the values from:

```text
.streamlit/secrets.example.toml
```

Replace the placeholders with:

- your real Streamlit URL
- Google OAuth Client ID
- Google OAuth Client Secret
- Stripe secret key
- the full Google service account JSON converted to TOML under `[gcp_service_account]`

Keep:

```toml
DASHBOARD_AUTH_ENABLED = "true"
DASHBOARD_ALLOWED_EMAIL_DOMAIN = "treble.ai"
```

## 5. Redeploy

After saving secrets, redeploy the app.

Only users signed in with a Google account ending in `@treble.ai` will be able to see the dashboard.

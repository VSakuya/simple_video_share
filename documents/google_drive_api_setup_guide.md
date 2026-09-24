# Setting Up Google Drive API for Headless Server Uploads (Personal Account)

This guide outlines the steps to configure the Google Drive API for automated, background file uploads on a headless server (e.g., Ubuntu VPS) using a personal Google account, while bypassing the 7-day token expiration limit.

## Step 1: Create Project and Enable API
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project dropdown in the top-left corner and select **New Project**. Name it and click **Create**.
3. With your new project selected, navigate to **APIs & Services** > **Library** in the left sidebar.
4. Search for **Google Drive API** and click **Enable**.

## Step 2: Configure the OAuth Consent Screen & Branding
Since Service Accounts do not have storage quotas for free personal Google accounts, we must use OAuth 2.0.

1. Navigate to **APIs & Services** > **OAuth consent screen** (or **Google Auth Platform** > **Audience** in the new UI).
2. Set the User Type to **External**.
3. Under the **Branding** section, fill in the required fields:
   * **App name:** (e.g., "Server Backup")
   * **User support email:** (Your email)
   * **Developer contact information:** (Your email)
   * **Homepage URL:** `https://example.com` *(Placeholder to bypass validation)*
   * **Privacy policy URL:** `https://example.com/privacy` *(Placeholder to bypass validation)*
   * Click **Save**.
4. Under the **Audience** (or Test Users) section, click **Add Users** and add the exact Google email address you will use for Google Drive.

## Step 3: Push to Production (Bypass 7-Day Expiration)
By default, "Testing" apps have tokens that expire in 7 days. We must set it to Production to make the refresh token permanent.
1. Go to the **Audience** (or Publishing status) page.
2. Click the **Push to production** (or **Publish app**) button.
3. If Google prompts you to "Prepare for verification" or submit verification materials, **ignore it completely**. As long as the status says **In production**, your personal script will not be subject to token expiration.

## Step 4: Create Credentials
1. Navigate to **APIs & Services** > **Credentials** (or **Clients**).
2. Click **+ CREATE CREDENTIALS** > **OAuth client ID**.
3. **Important:** Select **Desktop app** as the Application type (do NOT choose Web application, as it requires redirect URIs).
4. Click **Create** and download the resulting JSON file.
5. Rename the downloaded file to `client_secrets.json` and place it in a folder named `credentials/` inside your local project directory.

## Step 5: Generate the Persistent Token Locally
Since your server has no UI, you must perform the initial authentication on your local PC to generate a refresh token.

Create a Python script (e.g., `init_auth.py`) on your local machine:

```python
from pydrive2.drive import GoogleDrive
from pydrive2.auth import GoogleAuth

# Configure PyDrive to use your credentials folder
custom_settings = {
    "client_config_backend": "file",
    "client_config_file": "credentials/client_secrets.json"
}

gauth = GoogleAuth(settings=custom_settings)

# Attempt to load saved credentials, or prompt browser if missing/expired
gauth.LoadCredentialsFile("credentials/mycreds.txt")

if gauth.credentials is None:
    gauth.LocalWebserverAuth()
elif gauth.access_token_expired:
    gauth.Refresh()
else:
    gauth.Authorize()

# Save the permanent token for server use
gauth.SaveCredentialsFile("credentials/mycreds.txt")
print("Authentication successful! mycreds.txt has been generated.")
```

Run this script locally. A browser window will open. Log in with the account you added to the Test Users list and grant the permissions (click "Advanced" > "Go to..." if you see a safety warning). 
Once completed, a `mycreds.txt` file will appear in your `credentials/` folder.

## Step 6: Deploy to the Headless Server
1. Upload your Python scripts, the `credentials/client_secrets.json` file, and the newly generated `credentials/mycreds.txt` file to your headless server.
2. Ensure your server script is set up to load `mycreds.txt` just like the local script.
3. Because `mycreds.txt` contains a valid, production-level refresh token, the server will now automatically silently renew its access token without ever requiring a browser or UI.
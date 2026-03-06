#!/usr/bin/env python3
"""
CloudSurf - Google Drive Profile Backup/Restore
Uses service account credentials or OAuth tokens to sync profiles.

Usage:
  python3 gdrive_sync.py backup   # upload profiles to Drive
  python3 gdrive_sync.py restore  # download profiles from Drive
  python3 gdrive_sync.py list     # list backed up profiles

Setup:
  1. Go to console.cloud.google.com
  2. Create a project → Enable Google Drive API
  3. Create OAuth credentials (Desktop app) → Download JSON
  4. Set GDRIVE_CREDS_PATH env var to the JSON path
  OR
  4b. Run once to get tokens, they'll be saved to .gdrive_token.json
"""

import os, sys, json, zipfile, shutil
from pathlib import Path
from datetime import datetime

BASE_DIR     = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles"
CREDS_PATH   = os.environ.get("GDRIVE_CREDS_PATH", str(BASE_DIR / "gdrive_creds.json"))
TOKEN_PATH   = str(BASE_DIR / ".gdrive_token.json")
FOLDER_NAME  = "CloudSurf_Profiles"

def get_service():
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        print("Installing Google API libs...")
        os.system("pip3 install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client --quiet")
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

    SCOPES = ['https://www.googleapis.com/auth/drive.file']
    creds = None
    if Path(TOKEN_PATH).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDS_PATH).exists():
                print(f"ERROR: Credentials file not found at {CREDS_PATH}")
                print("Set GDRIVE_CREDS_PATH or place gdrive_creds.json in this directory.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)

def get_or_create_folder(service, name):
    results = service.files().list(
        q=f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()
    items = results.get('files', [])
    if items:
        return items[0]['id']
    meta = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
    folder = service.files().create(body=meta, fields='id').execute()
    return folder['id']

def backup(service=None):
    if service is None: service = get_service()
    folder_id = get_or_create_folder(service, FOLDER_NAME)
    
    profiles = [p for p in PROFILES_DIR.iterdir() if p.is_dir()]
    print(f"Backing up {len(profiles)} profiles to Google Drive folder '{FOLDER_NAME}'...")
    
    from googleapiclient.http import MediaFileUpload
    
    for profile_dir in profiles:
        zip_path = f"/tmp/{profile_dir.name}_backup.zip"
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in profile_dir.rglob('*'):
                if f.is_file() and 'SingletonLock' not in str(f):
                    zf.write(f, f.relative_to(profile_dir.parent))
        
        fname = f"{profile_dir.name}_{datetime.now().strftime('%Y%m%d')}.zip"
        media = MediaFileUpload(zip_path, mimetype='application/zip', resumable=True)
        
        # Check if file already exists (update vs create)
        existing = service.files().list(
            q=f"name='{fname}' and '{folder_id}' in parents and trashed=false",
            fields="files(id)"
        ).execute().get('files', [])
        
        if existing:
            service.files().update(fileId=existing[0]['id'], media_body=media).execute()
            print(f"  ✓ Updated: {fname}")
        else:
            service.files().create(
                body={'name': fname, 'parents': [folder_id]},
                media_body=media, fields='id'
            ).execute()
            print(f"  ✓ Uploaded: {fname}")
        
        os.remove(zip_path)
    
    print(f"\nBackup complete! {len(profiles)} profiles saved.")

def restore(service=None):
    if service is None: service = get_service()
    folder_id = get_or_create_folder(service, FOLDER_NAME)
    from googleapiclient.http import MediaIoBaseDownload
    import io
    
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, modifiedTime)"
    ).execute()
    files = sorted(results.get('files', []), key=lambda x: x['modifiedTime'], reverse=True)
    
    if not files:
        print("No backups found in Drive."); return
    
    print(f"Found {len(files)} backup files:")
    for i, f in enumerate(files):
        print(f"  [{i}] {f['name']} ({f['modifiedTime'][:10]})")
    
    choice = input("\nEnter file number to restore (or 'all'): ").strip()
    to_restore = files if choice == 'all' else [files[int(choice)]]
    
    for f in to_restore:
        print(f"Restoring {f['name']}...")
        request = service.files().get_media(fileId=f['id'])
        bio = io.BytesIO()
        dl = MediaIoBaseDownload(bio, request)
        done = False
        while not done:
            _, done = dl.next_chunk()
        
        zip_path = f"/tmp/{f['name']}"
        with open(zip_path, 'wb') as out:
            out.write(bio.getvalue())
        
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(PROFILES_DIR.parent)
        os.remove(zip_path)
        print(f"  ✓ Restored {f['name']}")
    
    print("\nRestore complete!")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "backup":   backup()
    elif cmd == "restore": restore()
    elif cmd == "list":
        svc = get_service()
        fid = get_or_create_folder(svc, FOLDER_NAME)
        r = svc.files().list(q=f"'{fid}' in parents and trashed=false", fields="files(name,modifiedTime,size)").execute()
        for f in r.get('files', []):
            print(f"  {f['name']}  ({int(f.get('size',0))//1024}KB)  {f['modifiedTime'][:10]}")
    else:
        print(__doc__)

# Firestore security

DeepGuard stores scan metadata at:

```text
users/{firebase_uid}/scans/{scan_id}
```

The root [`firestore.rules`](firestore.rules) file applies these controls:

- An authenticated client can read (`get` or `list`) only documents whose
  `{firebase_uid}` path segment equals its Firebase Authentication `uid`.
- Unauthenticated clients cannot read scan documents.
- A client cannot create, update, or delete scan documents, including derived
  voice and transcript result fields.
- User documents and every other collection/path are denied by default.

The Flask backend is the sole writer. It authenticates requests with Firebase
Authentication ID tokens, then uses the Firebase Admin SDK to write and delete
scan metadata. Admin SDK calls are trusted server-side calls and bypass
Firestore Security Rules; backend authorization and deployment credentials must
therefore remain server-side. Do not put a service-account JSON file in this
workspace or expose it to the browser.

## Deploy the rules

`firebase.json` points the Firebase CLI at the root rules file. Select the
project explicitly so a local alias or credentials file is not needed:

```powershell
firebase deploy --only firestore:rules --project your-firebase-project-id
```

The project must already have a Cloud Firestore database. The command deploys
only rules and does not change application code or data.

## Test locally with the Firestore emulator

From the repository root, start the emulator with a non-production project ID:

```powershell
firebase emulators:start --only firestore --project demo-deepguard
```

Use the Firebase Rules Unit Testing SDK or an authenticated client pointed at
the emulator to verify this matrix:

| Request | Expected result |
| --- | --- |
| Signed-in `user-a` reads `users/user-a/scans/scan-1` | Allowed |
| Signed-in `user-a` lists `users/user-a/scans` | Allowed |
| Signed-in `user-a` reads or lists `users/user-b/scans` | Denied |
| Unauthenticated client reads any scan path | Denied |
| Any client creates, updates, or deletes a scan document | Denied |
| Any client reads a user document or another collection | Denied |

For a repeatable rules-only test, load `firestore.rules` into
`initializeTestEnvironment({ firestore: { rules } })`, create authenticated
contexts for `user-a` and `user-b`, and assert the allowed/denied cases above.
Seed fixture documents through the emulator's Admin SDK/test environment, not
through the browser client, because production writes are backend-only.

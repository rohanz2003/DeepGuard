import sys
sys.path.insert(0, 'D:/Deepfake_Voice_Scam_Detector_MD_Files/backend')
from app import create_app

app = create_app()

with app.test_client() as client:
    # Test health endpoint
    resp = client.get('/api/health')
    print('Health:', resp.status_code, resp.get_json())

    # Test auth/me without token (should be 401)
    resp = client.get('/api/auth/me')
    print('Auth me (no token):', resp.status_code, resp.get_json())

    # Test with dummy bearer token (should be 503 or 401 depending on Firebase)
    resp = client.get('/api/auth/me', headers={'Authorization': 'Bearer dummy'})
    print('Auth me (dummy):', resp.status_code, resp.get_json())

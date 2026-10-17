from app import create_app
app = create_app()
print('SUCCESS: App created')
print('Firebase configured:', app.extensions['firebase'].configured)
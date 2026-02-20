from flask import Flask, render_template, jsonify, request, Response, send_file, redirect, url_for, session
import requests
from functools import lru_cache, wraps
from datetime import datetime, timedelta
import json
import os
import hashlib
from io import BytesIO

# pip install authlib
from authlib.integrations.flask_client import OAuth

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dayydesu-secret-key-2024!')

# Fix HTTPS redirect URI di Railway/production
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['PREFERRED_URL_SCHEME'] = 'https'
API_BASE = "https://www.sankavollerei.com"

# ============ GOOGLE OAUTH ============
GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '69561706021-08jeck9h7519dhajcbih6elme0nbichc.apps.googleusercontent.com')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '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')

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile',
        'token_endpoint_auth_method': 'client_secret_post'
    }
)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login')
def login():
    if 'user' in session:
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/login/google')
def login_google():
    try:
        redirect_uri = url_for('auth_callback', _external=True, _scheme='https')
        return google.authorize_redirect(redirect_uri)
    except Exception as e:
        print(f"OAuth redirect error: {e}")
        return f"Login error: {str(e)}", 500

@app.route('/auth/callback')
def auth_callback():
    try:
        token     = google.authorize_access_token()
        user_info = token.get('userinfo')
        if not user_info:
            resp = google.get('https://openidconnect.googleapis.com/v1/userinfo')
            user_info = resp.json()
        if user_info:
            session['user'] = {
                'name'   : user_info.get('name'),
                'email'  : user_info.get('email'),
                'picture': user_info.get('picture'),
                'sub'    : user_info.get('sub'),
            }
            return redirect(url_for('index'))
        return redirect(url_for('login'))
    except Exception as e:
        print(f"OAuth callback error: {e}")
        return f"Callback error: {str(e)}", 500

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/api/me')
def api_me():
    if 'user' in session:
        return jsonify({'status': 'success', 'data': session['user']})
    return jsonify({'status': 'error', 'message': 'Not logged in'}), 401

# ============ VERCEL SERVERLESS STORAGE ============
# Note: Vercel doesn't support SQLite or background tasks
# Using in-memory storage (will reset on cold start)
NOTIFICATIONS = []

# Simple cache for Vercel (in-memory)
CACHE = {}
CACHE_DURATION = {
    'home': 300,
    'ongoing': 300,
    'completed': 600,
    'schedule': 1800,
    'unlimited': 3600,
    'genre': 3600,
    'anime': 600,
    'episode': 300,
    'search': 300,
    'server': 60,
    'batch': 600
}

# ============ IMAGE CACHE CONFIGURATION ============
# Cache poster images selama 30 hari untuk menghindari rate limit API
IMAGE_CACHE_DIR = 'static/poster_cache'
IMAGE_CACHE_DURATION_DAYS = 30  # Cache 30 hari
IMAGE_CACHE = {}  # In-memory metadata {url: {'path': ..., 'cached_at': ..., 'hits': ...}}

# Create cache directory
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)

def get_from_cache(cache_key):
    """Get data from memory cache"""
    if cache_key in CACHE:
        cached_time, cache_type, data = CACHE[cache_key]
        max_age = CACHE_DURATION.get(cache_type, 300)
        
        if datetime.now() - cached_time < timedelta(seconds=max_age):
            return data
    return None

def save_to_cache(cache_key, data, cache_type='home'):
    """Save data to memory cache"""
    CACHE[cache_key] = (datetime.now(), cache_type, data)

def fetch_api(endpoint, cache_type='home'):
    """Fetch data from API with caching"""
    cache_key = f"{cache_type}_{endpoint}"
    
    # Try cache first
    cached_data = get_from_cache(cache_key)
    if cached_data is not None:
        return cached_data
    
    try:
        response = requests.get(f"{API_BASE}{endpoint}", timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Save to cache
        save_to_cache(cache_key, data, cache_type)
        
        return data
    except requests.exceptions.RequestException as e:
        return {"status": "error", "message": str(e)}

# ============ IMAGE PROXY FUNCTIONS ============

def get_image_cache_path(url):
    """Generate cache filename dari URL"""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(IMAGE_CACHE_DIR, f'{url_hash}.jpg')

def is_image_cached(url):
    """Check apakah image sudah di-cache DAN masih valid"""
    cache_path = get_image_cache_path(url)
    
    # Check file exists
    if not os.path.exists(cache_path):
        return False
    
    # Check in-memory metadata
    if url in IMAGE_CACHE:
        cached_at = IMAGE_CACHE[url].get('cached_at')
        if cached_at:
            cached_date = datetime.fromisoformat(cached_at)
            expiry_date = cached_date + timedelta(days=IMAGE_CACHE_DURATION_DAYS)
            
            # Jika masih valid, return True
            if datetime.now() < expiry_date:
                return True
    
    # Check file modification time as fallback
    file_stat = os.stat(cache_path)
    file_age = datetime.now() - datetime.fromtimestamp(file_stat.st_mtime)
    
    if file_age < timedelta(days=IMAGE_CACHE_DURATION_DAYS):
        # Update in-memory cache
        IMAGE_CACHE[url] = {
            'path': cache_path,
            'cached_at': datetime.fromtimestamp(file_stat.st_mtime).isoformat(),
            'hits': IMAGE_CACHE.get(url, {}).get('hits', 0)
        }
        return True
    
    return False

def cache_image(url, image_content):
    """Cache image ke disk"""
    cache_path = get_image_cache_path(url)
    
    try:
        # Save image to disk
        with open(cache_path, 'wb') as f:
            f.write(image_content)
        
        # Update in-memory metadata
        IMAGE_CACHE[url] = {
            'path': cache_path,
            'cached_at': datetime.now().isoformat(),
            'hits': 0,
            'size': len(image_content)
        }
        
        return cache_path
    except Exception as e:
        print(f"Error caching image: {e}")
        return None

# ============ IMAGE PROXY ROUTE ============

@app.route('/api/proxy-image', methods=['GET', 'OPTIONS'])
def proxy_image():
    """
    Image proxy endpoint dengan aggressive caching
    TIDAK akan membebani API karena:
    1. Cache 30 hari per image
    2. Serve dari disk cache, bukan fetch ulang
    3. Zero API calls untuk cached images
    
    Usage: /api/proxy-image?url=https://example.com/image.jpg
    """
    
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = Response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    image_url = request.args.get('url')
    
    if not image_url:
        return jsonify({'error': 'URL parameter required'}), 400
    
    # ===== CHECK CACHE FIRST (ZERO API CALLS) =====
    if is_image_cached(image_url):
        cache_path = get_image_cache_path(image_url)
        
        # Update hit count
        if image_url in IMAGE_CACHE:
            IMAGE_CACHE[image_url]['hits'] = IMAGE_CACHE[image_url].get('hits', 0) + 1
        
        # Serve from disk cache
        try:
            response = send_file(
                cache_path,
                mimetype='image/jpeg',
                as_attachment=False
            )
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['X-Cache-Status'] = 'HIT'
            response.headers['Cache-Control'] = f'public, max-age={60*60*24*30}'  # 30 days
            
            return response
        except Exception as e:
            print(f"Error serving cached image: {e}")
            # Continue to fetch if cache serve fails
    
    # ===== CACHE MISS - FETCH ONCE AND CACHE =====
    try:
        # Fetch image (ONLY ONCE per URL)
        print(f"Fetching image from: {image_url}")
        img_response = requests.get(
            image_url,
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        img_response.raise_for_status()
        
        image_content = img_response.content
        
        # Cache image for 30 days
        cache_image(image_url, image_content)
        
        # Return image
        response = Response(
            image_content,
            mimetype=img_response.headers.get('Content-Type', 'image/jpeg')
        )
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['X-Cache-Status'] = 'MISS'
        response.headers['Cache-Control'] = f'public, max-age={60*60*24*30}'  # 30 days
        
        return response
        
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Request timeout'}), 504
        
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch image: {str(e)}'}), 500
        
    except Exception as e:
        return jsonify({'error': f'Internal error: {str(e)}'}), 500

# ============ IMAGE CACHE STATS (Optional - untuk monitoring) ============

@app.route('/api/image-cache/stats')
def image_cache_stats():
    """Get cache statistics"""
    total_cached = len([f for f in os.listdir(IMAGE_CACHE_DIR) if f.endswith('.jpg')])
    total_size = sum(
        os.path.getsize(os.path.join(IMAGE_CACHE_DIR, f))
        for f in os.listdir(IMAGE_CACHE_DIR)
        if f.endswith('.jpg')
    )
    
    total_hits = sum(cache.get('hits', 0) for cache in IMAGE_CACHE.values())
    
    return jsonify({
        'status': 'success',
        'data': {
            'total_cached_images': total_cached,
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'total_hits': total_hits,
            'cache_duration_days': IMAGE_CACHE_DURATION_DAYS,
            'in_memory_entries': len(IMAGE_CACHE)
        }
    })

@app.route('/api/image-cache/clear', methods=['POST'])
def clear_image_cache():
    """Clear image cache (admin only - add authentication!)"""
    try:
        count = 0
        for filename in os.listdir(IMAGE_CACHE_DIR):
            if filename.endswith('.jpg'):
                file_path = os.path.join(IMAGE_CACHE_DIR, filename)
                os.remove(file_path)
                count += 1
        
        IMAGE_CACHE.clear()
        
        return jsonify({
            'status': 'success',
            'message': f'Cleared {count} cached images'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============ BOOKMARK PAGE (Client-side with localStorage) ============
@app.route('/bookmarks')
def bookmarks():
    """Bookmarks page - using client-side localStorage"""
    return render_template('bookmarks.html')

# ============ NOTIFICATION ROUTES (In-Memory) ============
@app.route('/api/notifications')
def api_notifications():
    """Get notifications"""
    return jsonify({
        'status': 'success',
        'data': {
            'notifications': NOTIFICATIONS[-10:],  # Last 10
            'unread_count': 0
        }
    })

@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    """Clear notifications"""
    NOTIFICATIONS.clear()
    return jsonify({'status': 'success'})

# ============ ANIME ROUTES ============
@app.route('/')
def index():
    data = fetch_api('/anime/home', 'home')
    return render_template('home.html', data=data)

@app.route('/api/home')
def api_home():
    data = fetch_api('/anime/home', 'home')
    return jsonify(data)

@app.route('/anime/<anime_id>')
def anime_detail(anime_id):
    """Anime detail page"""
    data = fetch_api(f'/anime/anime/{anime_id}', 'anime')
    return render_template('detail.html', anime_id=anime_id, data=data)

@app.route('/api/anime/<anime_id>')
def api_anime_detail(anime_id):
    data = fetch_api(f'/anime/anime/{anime_id}', 'anime')
    return jsonify(data)

@app.route('/ongoing')
def ongoing():
    page = request.args.get('page', 1, type=int)
    data = fetch_api(f'/anime/ongoing-anime?page={page}', 'ongoing')
    return render_template('ongoing.html', data=data)

@app.route('/completed')
def completed():
    page = request.args.get('page', 1, type=int)
    data = fetch_api(f'/anime/complete-anime?page={page}', 'completed')
    return render_template('completed.html', data=data)

@app.route('/schedule')
def schedule():
    data = fetch_api('/anime/schedule', 'schedule')
    return render_template('schedule.html', data=data)

@app.route('/api/schedule')
def api_schedule():
    data = fetch_api('/anime/schedule', 'schedule')
    return jsonify(data)

@app.route('/all-anime')
def all_anime():
    data = fetch_api('/anime/unlimited', 'unlimited')
    return render_template('all_anime.html', data=data)

@app.route('/api/all-anime')
def api_all_anime():
    data = fetch_api('/anime/unlimited', 'unlimited')
    return jsonify(data)

@app.route('/episode/<episode_id>')
def episode_detail(episode_id):
    data = fetch_api(f'/anime/episode/{episode_id}', 'episode')
    return render_template('episode.html', episode_id=episode_id, data=data)

@app.route('/api/episode/<episode_id>')
def api_episode_detail(episode_id):
    data = fetch_api(f'/anime/episode/{episode_id}', 'episode')
    return jsonify(data)

@app.route('/search')
def search():
    keyword = request.args.get('q', '')
    if not keyword:
        return render_template('home.html', data=fetch_api('/anime/home', 'home'))
    
    data = fetch_api(f'/anime/search/{keyword}', 'search')
    return render_template('search.html', keyword=keyword, data=data)

@app.route('/api/search/<keyword>')
def api_search(keyword):
    data = fetch_api(f'/anime/search/{keyword}', 'search')
    return jsonify(data)

@app.route('/api/server/<server_id>')
def api_server(server_id):
    data = fetch_api(f'/anime/server/{server_id}', 'server')
    return jsonify(data)

@app.route('/batch/<slug>')
def batch_download(slug):
    """Batch download page"""
    data = fetch_api(f'/anime/batch/{slug}', 'batch')
    return render_template('batch.html', slug=slug, data=data)

@app.route('/api/batch/<slug>')
def api_batch(slug):
    data = fetch_api(f'/anime/batch/{slug}', 'batch')
    return jsonify(data)

@app.route('/genres')
def genres():
    data = fetch_api('/anime/genre', 'genre')
    return render_template('genres.html', data=data)

@app.route('/api/genres')
def api_genres():
    data = fetch_api('/anime/genre', 'genre')
    return jsonify(data)

@app.route('/genre/<genre_id>')
def genre_detail(genre_id):
    data = fetch_api(f'/anime/genre/{genre_id}', 'genre')
    genre_name = genre_id.replace('-', ' ').title()
    return render_template('genre_detail.html', genre_id=genre_id, genre_name=genre_name, data=data)

@app.route('/api/genre/<genre_id>')
def api_genre_detail(genre_id):
    data = fetch_api(f'/anime/genre/{genre_id}', 'genre')
    return jsonify(data)

# Vercel needs this
if __name__ == '__main__':
    app.run(debug=True)

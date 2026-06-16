"""
CN31 Solver - Complete Railway Deployment
Uses yidun_proxyless.py + dun163.js + net.pkl
"""

import os
import sys
import time
import json
import threading
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Add /app to path
sys.path.insert(0, '/app')

# Import the CN31 solver
try:
    from yidun_proxyless import *
    import yidun_proxyless as solver
    SOLVER_AVAILABLE = True
    print("✅ CN31 Solver loaded successfully")
    
    # Check if dun163.js is accessible
    if os.path.exists('/app/dun163.js'):
        print("✅ dun163.js found")
    else:
        print("❌ dun163.js NOT found")
        SOLVER_AVAILABLE = False
    
    # Check if net.pkl exists
    if os.path.exists('/app/net.pkl'):
        size = os.path.getsize('/app/net.pkl')
        print(f"✅ net.pkl found ({size} bytes)")
    else:
        print("❌ net.pkl NOT found")
        SOLVER_AVAILABLE = False
        
except ImportError as e:
    SOLVER_AVAILABLE = False
    print(f"❌ CN31 Solver not available: {e}")
    import traceback
    traceback.print_exc()

# Global state
solver_running = False
solver_thread = None
generation_stats = {
    "status": "idle",
    "tokens_generated": 0,
    "start_time": None,
    "threads": 0
}

# Token storage
tokens_cache = []
token_lock = threading.Lock()
TOKEN_FILE = "/app/validated_tokens.txt"

def read_tokens_from_file():
    """Read tokens from the validated_tokens.txt file"""
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'r') as f:
                lines = f.readlines()
                return [line.strip() for line in lines if line.strip()]
        return []
    except Exception as e:
        print(f"Error reading tokens: {e}")
        return []

def get_new_tokens():
    """Get new tokens from file and add to cache"""
    global tokens_cache
    
    try:
        current_tokens = set(tokens_cache)
        file_tokens = set(read_tokens_from_file())
        new_tokens = file_tokens - current_tokens
        
        if new_tokens:
            with token_lock:
                tokens_cache.extend(list(new_tokens))
                generation_stats["tokens_generated"] = len(tokens_cache)
            print(f"✅ Added {len(new_tokens)} new tokens. Total: {len(tokens_cache)}")
        
        return list(new_tokens)
    except Exception as e:
        print(f"Error getting new tokens: {e}")
        return []

def run_solver_worker(threads=3):
    """Run the yidun_proxyless.py main function"""
    global solver_running, generation_stats
    
    print(f"🚀 Starting CN31 solver with {threads} threads...")
    generation_stats["status"] = "running"
    generation_stats["start_time"] = datetime.now().isoformat()
    generation_stats["threads"] = threads
    
    # Modify the NUM_THREADS in the solver
    solver.NUM_THREADS = threads
    
    try:
        # Run the main solver
        solver.main()
    except KeyboardInterrupt:
        print("⏹️ Solver stopped by user")
    except Exception as e:
        print(f"❌ Solver error: {e}")
        generation_stats["status"] = "error"
        generation_stats["error"] = str(e)
    finally:
        solver_running = False
        generation_stats["status"] = "stopped"

@app.route('/')
def status():
    """Get solver status"""
    get_new_tokens()
    
    return jsonify({
        "status": generation_stats["status"],
        "tokens_generated": generation_stats["tokens_generated"],
        "tokens_in_queue": len(tokens_cache),
        "threads": generation_stats["threads"],
        "start_time": generation_stats.get("start_time"),
        "solver_available": SOLVER_AVAILABLE,
        "files_ready": all([
            os.path.exists('/app/yidun_proxyless.py'),
            os.path.exists('/app/dun163.js'),
            os.path.exists('/app/net.pkl')
        ])
    })

@app.route('/health')
def health():
    """Health check"""
    return jsonify({
        "ok": True,
        "solver_available": SOLVER_AVAILABLE,
        "status": generation_stats["status"],
        "tokens_available": len(tokens_cache),
        "files": {
            "yidun_proxyless.py": os.path.exists('/app/yidun_proxyless.py'),
            "dun163.js": os.path.exists('/app/dun163.js'),
            "net.pkl": os.path.exists('/app/net.pkl')
        }
    })

@app.route('/debug/files')
def debug_files():
    """Debug - check all files"""
    import os
    files = {
        'yidun_proxyless.py': os.path.exists('/app/yidun_proxyless.py'),
        'dun163.js': os.path.exists('/app/dun163.js'),
        'net.pkl': os.path.exists('/app/net.pkl'),
        'validated_tokens.txt': os.path.exists('/app/validated_tokens.txt'),
    }
    
    sizes = {}
    for f in files:
        if files[f]:
            try:
                sizes[f] = os.path.getsize(f'/app/{f}')
            except:
                sizes[f] = 'error'
    
    return jsonify({
        'files': files,
        'sizes': sizes,
        'cwd': os.getcwd(),
        'all_files': os.listdir('/app') if os.path.exists('/app') else []
    })

@app.route('/debug/model')
def debug_model():
    """Debug - try loading model"""
    try:
        import torch
        import os
        
        # Check if net.pkl exists
        if not os.path.exists('/app/net.pkl'):
            return jsonify({
                'error': 'net.pkl not found',
                'files': os.listdir('/app') if os.path.exists('/app') else []
            })
        
        # Try to load model
        model_path = '/app/net.pkl'
        model = torch.load(model_path, map_location='cpu')
        
        return jsonify({
            'model_loaded': True,
            'model_keys': list(model.keys()) if hasattr(model, 'keys') else 'N/A',
            'model_path': model_path,
            'model_size': os.path.getsize(model_path)
        })
    except Exception as e:
        return jsonify({
            'error': str(e),
            'model_loaded': False
        })

@app.route('/start', methods=['POST'])
def start_solver():
    """Start the CN31 solver"""
    global solver_running, solver_thread, generation_stats
    
    if solver_running:
        return jsonify({"error": "Solver already running"}), 400
    
    if not SOLVER_AVAILABLE:
        return jsonify({"error": "CN31 Solver not available"}), 500
    
    # Check all required files
    required_files = ['yidun_proxyless.py', 'dun163.js', 'net.pkl']
    missing = [f for f in required_files if not os.path.exists(f'/app/{f}')]
    
    if missing:
        return jsonify({
            "error": f"Missing files: {missing}",
            "files": os.listdir('/app') if os.path.exists('/app') else []
        }), 500
    
    data = request.json or {}
    threads = min(data.get("threads", 3), 10)
    
    # Initialize model first
    try:
        model = initialize_global_model()
        if model is None:
            return jsonify({"error": "Failed to load model (check /debug/model)"}), 500
    except Exception as e:
        return jsonify({"error": f"Model error: {str(e)}"}), 500
    
    solver_running = True
    generation_stats["status"] = "starting"
    generation_stats["threads"] = threads
    
    # Start solver in background thread
    solver_thread = threading.Thread(
        target=run_solver_worker,
        args=(threads,),
        daemon=False
    )
    solver_thread.start()
    
    return jsonify({
        "message": "CN31 Solver started",
        "threads": threads,
        "status": "running"
    })

@app.route('/stop', methods=['POST'])
def stop_solver():
    """Stop the CN31 solver"""
    global solver_running
    
    solver_running = False
    generation_stats["status"] = "stopping"
    
    return jsonify({
        "message": "Stop signal sent",
        "tokens_generated": generation_stats["tokens_generated"]
    })

@app.route('/api/get-token', methods=['GET'])
def get_token():
    """Get a single token"""
    global tokens_cache
    
    get_new_tokens()
    
    with token_lock:
        if tokens_cache:
            token = tokens_cache.pop(0)
            return jsonify({
                "token": token,
                "remaining": len(tokens_cache)
            })
    
    return jsonify({"error": "No tokens available"}), 404

@app.route('/api/tokens', methods=['GET'])
def get_tokens():
    """Get multiple tokens"""
    global tokens_cache
    
    n = request.args.get('n', 5, type=int)
    n = min(n, 50)
    
    get_new_tokens()
    
    with token_lock:
        count = min(n, len(tokens_cache))
        result = tokens_cache[:count]
        tokens_cache = tokens_cache[count:]
        
        return jsonify({
            "tokens": result,
            "count": len(result),
            "remaining": len(tokens_cache)
        })

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 6000))
    
    print(f"""
🔐 CN31 Solver - Complete Railway Edition
─────────────────────────────────────────
Port       : {port}
Solver     : {'✅ Available' if SOLVER_AVAILABLE else '❌ Not Available'}
Files:
  - yidun_proxyless.py: {'✅' if os.path.exists('/app/yidun_proxyless.py') else '❌'}
  - dun163.js: {'✅' if os.path.exists('/app/dun163.js') else '❌'}
  - net.pkl: {'✅' if os.path.exists('/app/net.pkl') else '❌'}
""")
    
    app.run(host='0.0.0.0', port=port, debug=False)
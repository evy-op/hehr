"""
NECaptcha Solver - Railway Edition with CN31 Solver Integration
Uses the yidun_proxyless.py solver engine
"""

import os
import sys
import time
import json
import threading
import logging
import warnings
from datetime import datetime
from flask import Flask, jsonify, request
import requests

# Suppress warnings
warnings.filterwarnings('ignore')

app = Flask(__name__)

# Global state
tokens = []
token_lock = threading.Lock()
generation_running = False
generation_stats = {"status": "idle", "generated": 0, "total": 0}
solver_threads = []

# Import the CN31 solver
try:
    from yidun_proxyless import Dun163, initialize_global_model, TOKEN_OUTPUT_FILE
    SOLVER_AVAILABLE = True
    print("✅ CN31 Solver loaded successfully")
except ImportError as e:
    SOLVER_AVAILABLE = False
    print(f"❌ CN31 Solver not available: {e}")

# Configuration
ID = "fef5c67c39074e9d845f4bf579cc07af"
REFERER = "https://mtacc.mobilelegends.com/"
FP_H = "mtacc.mobilelegends.com"

def run_solver_worker(thread_id, num_tokens=10):
    """Run the CN31 solver to generate tokens"""
    global generation_stats, tokens
    
    try:
        # Initialize the solver
        import random
        from fake_useragent import UserAgent
        
        ua = UserAgent().random
        
        dun = Dun163(
            id_=ID,
            referer=REFERER,
            fp_h=FP_H,
            ua=ua,
            thread_id=thread_id
        )
        
        success_count = 0
        
        while generation_running and success_count < num_tokens:
            try:
                # Run one solve attempt
                success = dun.run()
                
                if success:
                    # Get the token from the file or from the solver
                    # The solver saves to file, we'll read it
                    with open(TOKEN_OUTPUT_FILE, 'r') as f:
                        lines = f.readlines()
                        if lines:
                            latest_token = lines[-1].strip()
                            if latest_token:
                                with token_lock:
                                    tokens.append(latest_token)
                                    generation_stats["generated"] += 1
                                    generation_stats["total"] = len(tokens)
                                success_count += 1
                                print(f"[T{thread_id}] ✅ Token #{success_count}: {latest_token[:40]}...")
                
                time.sleep(random.uniform(0.5, 1.5))
                
            except Exception as e:
                print(f"[T{thread_id}] Error: {e}")
                time.sleep(2)
                
    except Exception as e:
        print(f"[T{thread_id}] Worker error: {e}")

# Flask Routes
@app.route('/')
def status():
    return jsonify({
        "status": generation_stats["status"],
        "generated": generation_stats["generated"],
        "total": generation_stats["total"],
        "solver_available": SOLVER_AVAILABLE,
        "tokens_in_queue": len(tokens)
    })

@app.route('/health')
def health():
    return jsonify({
        "ok": True,
        "solver_available": SOLVER_AVAILABLE,
        "status": generation_stats["status"]
    })

@app.route('/start', methods=['POST'])
def start_generation():
    global generation_running, generation_stats, solver_threads
    
    if generation_running:
        return jsonify({"error": "Already running"}), 400
    
    if not SOLVER_AVAILABLE:
        return jsonify({"error": "CN31 Solver not available"}), 500
    
    data = request.json or {}
    threads = min(data.get("threads", 2), 5)
    num_tokens = data.get("num_tokens", 50)
    
    # Initialize model first
    try:
        from yidun_proxyless import initialize_global_model
        model = initialize_global_model()
        if not model:
            return jsonify({"error": "Failed to load model"}), 500
    except Exception as e:
        return jsonify({"error": f"Model error: {e}"}), 500
    
    generation_running = True
    generation_stats["status"] = "running"
    generation_stats["generated"] = 0
    
    # Start workers
    solver_threads = []
    for i in range(threads):
        t = threading.Thread(
            target=run_solver_worker, 
            args=(i+1, num_tokens // threads + 1),
            daemon=True
        )
        solver_threads.append(t)
        t.start()
    
    return jsonify({
        "message": "Generation started",
        "threads": threads,
        "num_tokens": num_tokens
    })

@app.route('/stop', methods=['POST'])
def stop_generation():
    global generation_running, generation_stats
    generation_running = False
    generation_stats["status"] = "idle"
    return jsonify({"message": "Stop signal sent"})

@app.route('/api/get-token', methods=['GET'])
def get_token():
    with token_lock:
        if tokens:
            token = tokens.pop(0)
            return jsonify({
                "token": token,
                "remaining": len(tokens)
            })
    return jsonify({"error": "No tokens"}), 404

@app.route('/api/tokens', methods=['GET'])
def get_tokens():
    n = request.args.get('n', 5, type=int)
    n = min(n, 50)
    
    with token_lock:
        count = min(n, len(tokens))
        result = tokens[:count]
        # Remove retrieved tokens
        for _ in range(count):
            if tokens:
                tokens.pop(0)
        
        return jsonify({
            "tokens": result,
            "count": len(result),
            "remaining": len(tokens)
        })

@app.route('/api/status', methods=['GET'])
def api_status():
    return status()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 6000))
    
    # Initialize model on startup
    if SOLVER_AVAILABLE:
        try:
            from yidun_proxyless import initialize_global_model
            initialize_global_model()
            print("✅ Model initialized")
        except Exception as e:
            print(f"❌ Model initialization failed: {e}")
    
    print(f"""
🔐 NECaptcha CN31 Solver - Railway Edition
─────────────────────────────────────────
Port       : {port}
Solver     : {'✅ Available' if SOLVER_AVAILABLE else '❌ Not Available'}
Model      : {'✅ Loaded' if SOLVER_AVAILABLE else '❌ Not Loaded'}
""")
    
    app.run(host='0.0.0.0', port=port, debug=False)
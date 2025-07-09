from flask import Flask, request, render_template, jsonify, redirect, url_for, session, flash
import os
from dotenv import load_dotenv
import google.generativeai as genai
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from functools import wraps
from datetime import datetime
from markupsafe import Markup
import markdown
# ✅ Load environment variables from .env file
# Only load .env locally (Render already has env vars set)
# Only load .env locally (Render already sets env variables)
if os.environ.get("RENDER") is None:
    load_dotenv()

DB_CONFIG = {
    'host': os.environ.get("DB_HOST"),
    'database': os.environ.get("DB_NAME"),
    'user': os.environ.get("DB_USER"),
    'password': os.environ.get("DB_PASS"),
    'port': os.environ.get("DB_PORT", 5432)
}
# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = secrets.token_hex(16)  # Generate a secure secret key for sessions

# Add markdown filter
@app.template_filter('markdown')
def markdown_filter(text):
    return Markup(markdown.markdown(text, extensions=['fenced_code', 'codehilite']))

# Configure Gemini API
API_KEY = "AIzaSyChFYnEka9jiBTHdTMK2jLH75X7K55ot4I"  # Replace with your actual API key
os.environ['GOOGLE_API_KEY'] = API_KEY
genai.configure(api_key=API_KEY)

# Initialize model
try:
    model = genai.GenerativeModel(
        'gemini-1.5-flash',
        system_instruction="""You are a helpful AI assistant. You maintain context from previous messages in the conversation. 
        When users ask follow-up questions like "explain in detail", "give me more info", "elaborate", etc., 
        refer back to the previous topics discussed in the conversation."""
    )
    print("Gemini model initialized successfully")
except Exception as e:
    print(f"Error initializing model: {str(e)}")
    model = None

# Database connection function
def get_db_connection():
    try:
        conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print(f"Database connection error: {str(e)}")
        return None

# Test database connection
def test_db_connection():
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT version();')
                version = cur.fetchone()
                print(f"Database connected successfully: {version['version']}")
                return True
        except Exception as e:
            print(f"Database test error: {str(e)}")
            return False
        finally:
            conn.close()
    return False

# Initialize database tables
def init_db():
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                # Create users table
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        username VARCHAR(100) UNIQUE NOT NULL,
                        password VARCHAR(255) NOT NULL,
                        email VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create chat_sessions table
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS chat_sessions (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create chat_messages table
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS chat_messages (
                        id SERIAL PRIMARY KEY,
                        session_id INTEGER REFERENCES chat_sessions(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                        message TEXT NOT NULL,
                        response TEXT NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Create indexes for better performance
                cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_user_id ON chat_messages(user_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id ON chat_sessions(user_id)')
                
                conn.commit()
                print("Database tables initialized successfully")
                return True
        except Exception as e:
            print(f"Error initializing database: {str(e)}")
            conn.rollback()
            return False
        finally:
            conn.close()
    return False

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/', methods=['GET'])
@login_required
def home():
    user_id = session.get('user_id')
    username = session.get('username')
    current_session_id = session.get('session_id')
    
    # Get user's chat sessions with messages
    conn = get_db_connection()
    chat_sessions = []
    current_session_messages = []
    
    if conn:
        try:
            with conn.cursor() as cur:
                # Get all sessions for this user
                cur.execute('''
                    SELECT s.id AS session_id, s.created_at AS session_date
                    FROM chat_sessions s
                    WHERE s.user_id = %s
                    ORDER BY s.created_at DESC
                ''', (user_id,))
                
                sessions = cur.fetchall()
                
                for session_row in sessions:
                    session_id = session_row['session_id']
                    session_date = session_row['session_date'].strftime('%Y-%m-%d %H:%M')
                    
                    # Get messages for this session
                    cur.execute('''
                        SELECT message as question, response, to_char(timestamp, 'HH24:MI') as timestamp
                        FROM chat_messages
                        WHERE session_id = %s
                        ORDER BY timestamp ASC
                    ''', (session_id,))
                    
                    messages = cur.fetchall()
                    
                    # Store messages for the current session
                    if session_id == current_session_id:
                        current_session_messages = messages
                    
                    chat_sessions.append({
                        'session_id': session_id,
                        'session_date': session_date,
                        'messages': messages
                    })
        except Exception as e:
            print(f"Error fetching chat sessions: {str(e)}")
        finally:
            conn.close()
    
    return render_template('index.html', 
                         chat_sessions=chat_sessions,
                         chat_history=current_session_messages,  # Pass messages for current session
                         username=username,
                         current_session_id=current_session_id)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            flash('Please enter both username and password', 'error')
            return render_template('login.html')
        
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute('SELECT * FROM users WHERE username = %s', (username,))
                    user = cur.fetchone()
                    
                    if user and check_password_hash(user['password'], password):
                        session['user_id'] = user['id']
                        session['username'] = user['username']
                        
                        # Get the most recent session for this user
                        cur.execute('''
                            SELECT id FROM chat_sessions 
                            WHERE user_id = %s 
                            ORDER BY created_at DESC 
                            LIMIT 1
                        ''', (user['id'],))
                        existing_session = cur.fetchone()
                        
                        if existing_session:
                            # Use existing session
                            session_id = existing_session['id']
                        else:
                            # Create a new session if none exists
                            cur.execute(
                                'INSERT INTO chat_sessions (user_id) VALUES (%s) RETURNING id',
                                (user['id'],))
                            session_id = cur.fetchone()['id']
                        
                        session['session_id'] = session_id
                        conn.commit()
                        
                        next_page = request.args.get('next')
                        if next_page:
                            return redirect(next_page)
                        return redirect(url_for('home'))
                    else:
                        flash('Invalid username or password', 'error')
            except Exception as e:
                flash(f'Login error: {str(e)}', 'error')
                print(f"Login error: {str(e)}")
            finally:
                conn.close()
        else:
            flash('Database connection error', 'error')
        
        return render_template('login.html')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        email = request.form.get('email', '').strip()
        
        if not username or not password:
            flash('Please enter both username and password', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long', 'error')
            return render_template('register.html')
        
        hashed_password = generate_password_hash(password)
        
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    # Check if username already exists
                    cur.execute('SELECT id FROM users WHERE username = %s', (username,))
                    if cur.fetchone():
                        flash('Username already exists', 'error')
                        return render_template('register.html')
                    
                    # Check if email already exists (if provided)
                    if email:
                        cur.execute('SELECT id FROM users WHERE email = %s', (email,))
                        if cur.fetchone():
                            flash('Email already exists', 'error')
                            return render_template('register.html')
                    
                    # Insert new user
                    cur.execute(
                        'INSERT INTO users (username, password, email) VALUES (%s, %s, %s) RETURNING id',
                        (username, hashed_password, email if email else None)
                    )
                    user_id = cur.fetchone()['id']
                    
                    # Create a new session for this user
                    cur.execute(
                        'INSERT INTO chat_sessions (user_id) VALUES (%s) RETURNING id',
                        (user_id,))
                    session_id = cur.fetchone()['id']
                    session['session_id'] = session_id
                    
                    conn.commit()
                    
                    # Log user in
                    session['user_id'] = user_id
                    session['username'] = username
                    
                    flash('Registration successful! Welcome to Gemini Chat!', 'success')
                    return redirect(url_for('home'))
            except Exception as e:
                conn.rollback()
                flash(f'Registration error: {str(e)}', 'error')
                print(f"Registration error: {str(e)}")
            finally:
                conn.close()
        else:
            flash('Database connection error', 'error')
        
        return render_template('register.html')
    
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out successfully', 'success')
    return redirect(url_for('login'))

@app.route('/ask', methods=['POST'])
@login_required
def ask():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    user_id = session.get('user_id')
    session_id = session.get('session_id')
    data = request.get_json()
    user_input = data.get('message', '').strip()
    
    if not user_input:
        return jsonify({"error": "No message provided"}), 400
    
    if user_input.lower() == 'exit':
        return jsonify({"response": "Chat session ended."})
    
    if not model:
        return jsonify({"error": "AI model not initialized. Please check API key and model availability."})
    
    try:
        # Get the full chat history for this session
        conn = get_db_connection()
        chat_history = []
        
        if conn:
            try:
                with conn.cursor() as cur:
                    # Get all messages for this session
                    cur.execute('''
                        SELECT message, response
                        FROM chat_messages
                        WHERE session_id = %s
                        ORDER BY timestamp ASC
                    ''', (session_id,))
                    
                    for row in cur.fetchall():
                        chat_history.append({
                            'role': 'user',
                            'parts': [row['message']]
                        })
                        chat_history.append({
                            'role': 'model',
                            'parts': [row['response']]
                        })
            except Exception as e:
                print(f"Error retrieving chat history: {str(e)}")
        
        # Create chat session with full history
        chat_session = model.start_chat(history=chat_history)
        
        # Send the current message
        response = chat_session.send_message(user_input)
        response_text = response.text
        
        # Save message and response to database
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute('''
                        INSERT INTO chat_messages (session_id, user_id, message, response)
                        VALUES (%s, %s, %s, %s)
                    ''', (session_id, user_id, user_input, response_text))
                    conn.commit()
            except Exception as e:
                print(f"Error saving message: {str(e)}")
                conn.rollback()
            finally:
                if conn:
                    conn.close()
        
        return jsonify({
            "response": response_text,
            "timestamp": time.strftime('%H:%M')
        })
        
    except Exception as e:
        error_message = f"Error: {str(e)}"
        print(f"Detailed error: {e}")
        return jsonify({"error": error_message})

@app.route('/switch_session', methods=['POST'])
@login_required
def switch_session():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.get_json()
    session_id = data.get('session_id')
    
    if not session_id:
        return jsonify({"error": "No session ID provided"}), 400
    
    # Verify the session belongs to the current user
    user_id = session.get('user_id')
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT id FROM chat_sessions 
                    WHERE id = %s AND user_id = %s
                ''', (session_id, user_id))
                if not cur.fetchone():
                    return jsonify({"error": "Invalid session"}), 400
                
                # Update current session in the user's session
                session['session_id'] = session_id
                
                # Get messages for this session
                cur.execute('''
                    SELECT message as question, response, to_char(timestamp, 'HH24:MI') as timestamp
                    FROM chat_messages
                    WHERE session_id = %s
                    ORDER BY timestamp ASC
                ''', (session_id,))
                messages = cur.fetchall()
                
                return jsonify({
                    "status": "success",
                    "messages": messages
                })
        except Exception as e:
            print(f"Error switching session: {str(e)}")
            return jsonify({"error": str(e)}), 500
        finally:
            if conn:
                conn.close()
    return jsonify({"error": "Database error"}), 500

@app.route('/clear_history', methods=['POST'])
@login_required
def clear_history():
    user_id = session.get('user_id')
    
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                # Delete all messages for this user
                cur.execute('DELETE FROM chat_messages WHERE user_id = %s', (user_id,))
                # Delete all sessions for this user
                cur.execute('DELETE FROM chat_sessions WHERE user_id = %s', (user_id,))
                conn.commit()
                
                # Create a new session for the user
                cur.execute(
                    'INSERT INTO chat_sessions (user_id) VALUES (%s) RETURNING id',
                    (user_id,))
                session_id = cur.fetchone()['id']
                session['session_id'] = session_id
                conn.commit()
                
                print(f"Cleared history and created new session for user {user_id}")
        except Exception as e:
            print(f"Error clearing history: {str(e)}")
            conn.rollback()
        finally:
            if conn:
                conn.close()
    
    return jsonify({"status": "success", "message": "Chat history cleared"})

@app.route('/new_session', methods=['POST'])
@login_required
def new_session():
    user_id = session.get('user_id')
    
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                # Create a new session for this user
                cur.execute(
                    'INSERT INTO chat_sessions (user_id) VALUES (%s) RETURNING id',
                    (user_id,))
                session_id = cur.fetchone()['id']
                session['session_id'] = session_id
                conn.commit()
                
                print(f"Created new session {session_id} for user {user_id}")
        except Exception as e:
            print(f"Error creating new session: {str(e)}")
            conn.rollback()
        finally:
            if conn:
                conn.close()
    
    return jsonify({"status": "success", "session_id": session_id})

@app.route('/health')
def health_check():
    """Health check endpoint to verify system status"""
    db_status = test_db_connection()
    model_status = model is not None
    
    return jsonify({
        "status": "healthy" if db_status and model_status else "unhealthy",
        "database": "connected" if db_status else "disconnected",
        "ai_model": "initialized" if model_status else "not initialized",
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
    })

# Initialize database and test connections on startup
if __name__ == '__main__':
    print("Starting Gemini Chat Application...")
    
    # Test database connection
    if test_db_connection():
        print("✓ Database connection successful")
        
        # Initialize database tables
        if init_db():
            print("✓ Database tables initialized")
        else:
            print("✗ Failed to initialize database tables")
    else:
        print("✗ Database connection failed")
        print("Please check your database configuration in DB_CONFIG")
    
    # Test AI model
    if model:
        print("✓ Gemini AI model initialized")
    else:
        print("✗ Failed to initialize Gemini AI model")
        print("Please check your API key")
    
    print("\nStarting Flask server...")
    print("Visit http://localhost:5000 to access the application")
    print("Visit http://localhost:5000/health to check system status")
    
    app.run(debug=True, host='0.0.0.0', port=5000)

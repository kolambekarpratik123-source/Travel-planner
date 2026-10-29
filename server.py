"""VoyageAI travel planner: a Flask app that renders every page on the server (no JavaScript)."""
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from functools import wraps

try:
    from dotenv import load_dotenv  # optional: lets you keep GEMINI_API_KEY in a .env file
    load_dotenv()
except ImportError:
    pass

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import gemini_service
import planner
from gemini_service import GeminiError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, 'travel_planner.db')

DURATIONS = [(1, '1 Day Express'), (2, '2 Days Weekend'), (3, '3 Days Standard'),
             (4, '4 Days Standard'), (5, '5 Days Extended'), (7, '7 Days Week-long')]
BUDGETS = [('Budget', 'fa-coins', 'Budget'), ('Standard', 'fa-credit-card', 'Standard'),
           ('Luxury', 'fa-crown', 'Luxury'), ('Custom', 'fa-sliders', 'Custom')]
STYLES = [('Culture', 'fa-landmark', 'Culture & Heritage'),
          ('Adventure', 'fa-mountain-sun', 'Adventure & Nature'),
          ('Relaxation', 'fa-spa', 'Relaxation'),
          ('Foodie', 'fa-utensils', 'Food & Culinary'),
          ('Urban & Landmarks', 'fa-building', 'Urban Explorer')]
COMPANIONS = [('Solo', 'fa-user', 'Solo Traveller'), ('Couple', 'fa-heart', 'Couple'),
              ('Friends', 'fa-user-group', 'Friends'), ('Family', 'fa-people-roof', 'Family')]

LOADING_STEPS = [
    (0, 'Locating coordinates...', 'Finding nearby spots and hotels...'),
    (12, 'Analyzing spots...', 'Curating sights based on your style and companions...'),
    (30, 'Connecting routes...', 'Plotting public transit and taxi travel paths...'),
    (50, 'Formatting itinerary...', 'Putting your days in order...'),
    (90, 'Almost there...', 'Long trips take a little longer. Hang tight!'),
]

app = Flask(__name__)
# Set FLASK_SECRET_KEY to keep people logged in across restarts.
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.jinja_env.globals.update(valid_image=planner.valid_image)


# ---------------------------------------------------------------- database

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_FILE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            destination TEXT NOT NULL,
            duration INTEGER NOT NULL,
            budget TEXT NOT NULL,
            style TEXT NOT NULL,
            itinerary_data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        -- Plans being generated or just generated, before the user saves them.
        CREATE TABLE IF NOT EXISTS generated_plans (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,           -- pending | done | error
            source TEXT,                    -- ai | demo | fallback
            plan_json TEXT,
            error TEXT,
            created_at REAL NOT NULL
        );
    ''')
    # Drop unsaved plans older than a day.
    conn.execute('DELETE FROM generated_plans WHERE created_at < ?', (time.time() - 86400,))
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------- security helpers

def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def check_csrf():
    if request.method == 'POST':
        sent = request.form.get('_csrf', '')
        if not sent or not secrets.compare_digest(sent, session.get('_csrf', '')):
            abort(400, 'Form expired. Go back, reload the page and try again.')


def current_user():
    if 'user_id' not in session:
        return None
    return {'id': session['user_id'], 'username': session['username'], 'name': session['name']}


@app.context_processor
def inject_globals():
    return {'user': current_user(), 'ai_enabled': gemini_service.has_api_key()}


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('login', next=request.full_path))
        return view(*args, **kwargs)
    return wrapper


def safe_next(target):
    """Only allow redirects back into this site."""
    if target and target.startswith('/') and not target.startswith('//'):
        return target
    return url_for('index')


# ---------------------------------------------------------------- plan generation

def _store_plan(plan_id, status, source=None, plan=None, error=None):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        'INSERT OR REPLACE INTO generated_plans (id, status, source, plan_json, error, created_at) '
        'VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM generated_plans WHERE id = ?), ?))',
        (plan_id, status, source, json.dumps(plan) if plan is not None else None, error, plan_id, time.time()))
    conn.commit()
    conn.close()


def _generate_in_background(plan_id, destination, duration, budget, style):
    try:
        plan = gemini_service.generate_plan(destination, duration, budget, style)
        planner.drop_broken_images(plan)
        planner.attach_photos(plan)
        _store_plan(plan_id, 'done', 'ai', plan)
    except GeminiError as e:
        template = planner.find_demo_template(destination)
        if template:
            plan = planner.attach_photos(planner.adapt_demo_plan(template, duration, budget, style))
            _store_plan(plan_id, 'done', 'fallback', plan, error=str(e))
        else:
            _store_plan(plan_id, 'error', error=str(e))
    except Exception:  # never leave a plan stuck on "pending"
        app.logger.exception('Plan generation failed')
        _store_plan(plan_id, 'error', error='Something went wrong while generating the plan.')


def _load_generated(plan_id):
    row = db().execute('SELECT * FROM generated_plans WHERE id = ?', (plan_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _day_from_request(plan):
    days = len(plan.get('itinerary') or []) or 1
    try:
        day = int(request.args.get('day', 1))
    except ValueError:
        day = 1
    return min(max(day, 1), days)


def _render_plan(plan, source, *, plan_id=None, trip_id=None, notice=None):
    plan = planner.ensure_coordinates(plan)
    day = _day_from_request(plan)
    day_plan = next((d for d in plan.get('itinerary') or [] if d.get('day') == day), None)
    if day_plan is None and plan.get('itinerary'):
        day_plan = plan['itinerary'][day - 1]
    schedule = (day_plan or {}).get('schedule') or []

    # Extra details for each stop, from the matching spot or hotel.
    def details_for(stop_name):
        name = (stop_name or '').lower()
        for item in (plan.get('spots') or []) + (plan.get('hotels') or []):
            other = (item.get('name') or '').lower()
            if other and (name in other or other in name):
                return item
        return None

    stops = [{'stop': s, 'details': details_for(s.get('spot'))} for s in schedule]
    hero_image = planner.valid_image(plan.get('hero_image'))
    return render_template(
        'plan.html', plan=plan, source=source, day=day, stops=stops,
        route_map=planner.route_map(plan, schedule), hero_image=hero_image,
        plan_id=plan_id, trip_id=trip_id, notice=notice,
    )


# ---------------------------------------------------------------- pages

@app.route('/')
def index():
    return render_template('index.html', **_form_context())


def _form_context(**overrides):
    ctx = {
        'presets': planner.PRESET_GROUPS,
        'all_presets': planner.POPULAR_PRESETS,
        'durations': DURATIONS, 'budgets': BUDGETS, 'styles': STYLES, 'companions': COMPANIONS,
        'form': {
            'destination': request.args.get('destination', ''),
            'duration': 3, 'budget': 'Standard', 'custom_budget': '',
            'style': 'Culture', 'companion': 'Solo',
        },
    }
    ctx['form'].update(overrides)
    return ctx


@app.route('/generate', methods=['POST'])
def generate():
    f = request.form
    destination = (f.get('destination') or '').strip()
    budget_choice = f.get('budget') or 'Standard'
    custom_budget = (f.get('custom_budget') or '').strip()
    style = f.get('style') or 'Culture'
    companion = f.get('companion') or 'Solo'
    try:
        duration = int(f.get('duration', 3))
    except ValueError:
        duration = 3

    form_values = dict(destination=destination, duration=duration, budget=budget_choice,
                       custom_budget=custom_budget, style=style, companion=companion)
    error = None
    if not destination:
        error = 'Tell us where you want to go.'
    elif len(destination) > 200:
        error = 'Destination must be 200 characters or fewer.'
    elif duration not in dict(DURATIONS):
        error = 'Pick a trip duration from the list.'
    elif budget_choice not in [b[0] for b in BUDGETS] or style not in [s[0] for s in STYLES]:
        error = 'Pick a budget class and travel style.'
    elif budget_choice == 'Custom' and not custom_budget:
        error = 'Enter your custom budget, e.g. "₹20,000 total".'
    elif len(custom_budget) > 100:
        error = 'Custom budget must be 100 characters or fewer.'
    if error:
        flash(error, 'error')
        return render_template('index.html', **_form_context(**form_values)), 400

    budget = custom_budget if budget_choice == 'Custom' else budget_choice
    plan_id = uuid.uuid4().hex

    if gemini_service.has_api_key():
        _store_plan(plan_id, 'pending')
        threading.Thread(target=_generate_in_background,
                         args=(plan_id, destination, duration, budget, style), daemon=True).start()
        return redirect(url_for('show_plan', plan_id=plan_id))

    template = planner.find_demo_template(destination)
    if not template:
        flash('No Gemini API key is set on the server, so only the pre-loaded demo cities work. '
              'Pick one of the cities below, or set GEMINI_API_KEY to plan any destination.', 'error')
        return render_template('index.html', **_form_context(**form_values)), 400
    plan = planner.attach_photos(planner.adapt_demo_plan(template, duration, budget, style), timeout=4)
    _store_plan(plan_id, 'done', 'demo', plan)
    return redirect(url_for('show_plan', plan_id=plan_id))


@app.route('/plan/<plan_id>')
def show_plan(plan_id):
    row = _load_generated(plan_id)
    if row['status'] == 'pending':
        elapsed = int(time.time() - row['created_at'])
        title, status = LOADING_STEPS[0][1:]
        for start, t, s in LOADING_STEPS:
            if elapsed >= start:
                title, status = t, s
        return render_template('loading.html', elapsed=elapsed, title=title, status=status)
    if row['status'] == 'error':
        flash(f'Live AI generation failed: {row["error"]} Try again in a moment, '
              'or pick one of the pre-loaded cities.', 'error')
        return redirect(url_for('index'))

    notice = None
    if row['source'] == 'fallback':
        notice = ('The live AI was unavailable, so this is the pre-loaded offline plan for this city.')
    return _render_plan(json.loads(row['plan_json']), row['source'], plan_id=plan_id, notice=notice)


@app.route('/plan/<plan_id>/save', methods=['POST'])
def save_plan(plan_id):
    if 'user_id' not in session:
        flash('Log in to save this trip. You will come straight back to it.', 'warning')
        return redirect(url_for('login', next=url_for('show_plan', plan_id=plan_id)))
    row = _load_generated(plan_id)
    if row['status'] != 'done':
        abort(409)
    plan = json.loads(row['plan_json'])
    cur = db().execute(
        'INSERT INTO trips (user_id, destination, duration, budget, style, itinerary_data) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (session['user_id'], plan.get('destination', ''), plan.get('duration') or 1,
         str(plan.get('budget', '')), str(plan.get('style', '')), row['plan_json']))
    db().commit()
    flash('Trip saved to your account.', 'success')
    return redirect(url_for('show_trip', trip_id=cur.lastrowid))


# ---------------------------------------------------------------- saved trips

def _own_trip(trip_id):
    row = db().execute('SELECT * FROM trips WHERE id = ?', (trip_id,)).fetchone()
    if row is None:
        abort(404)
    if row['user_id'] != session['user_id']:
        abort(403)
    return row


@app.route('/trips')
@login_required
def trips():
    q = (request.args.get('q') or '').strip()
    budget = request.args.get('budget') or 'All'
    sql = 'SELECT * FROM trips WHERE user_id = ?'
    params = [session['user_id']]
    if q:
        sql += ' AND destination LIKE ?'
        params.append(f'%{q}%')
    if budget != 'All':
        sql += ' AND LOWER(budget) = LOWER(?)'
        params.append(budget)
    rows = db().execute(sql + ' ORDER BY created_at DESC', params).fetchall()
    return render_template('trips.html', trips=rows, q=q, budget=budget, **_form_context())


@app.route('/trips/<int:trip_id>')
@login_required
def show_trip(trip_id):
    row = _own_trip(trip_id)
    try:
        plan = json.loads(row['itinerary_data'])
    except ValueError:
        abort(500)
    return _render_plan(plan, 'saved', trip_id=trip_id)


@app.route('/trips/<int:trip_id>/delete', methods=['POST'])
@login_required
def delete_trip(trip_id):
    row = _own_trip(trip_id)
    db().execute('DELETE FROM trips WHERE id = ?', (trip_id,))
    db().commit()
    flash(f'Deleted your trip to {row["destination"]}.', 'success')
    return redirect(url_for('trips'))


# ---------------------------------------------------------------- accounts

@app.route('/login', methods=['GET', 'POST'])
def login():
    next_url = request.values.get('next', '')
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip().lower()
        password = (request.form.get('password') or '').strip()
        user = db().execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            csrf = session.get('_csrf')
            session.clear()
            session['_csrf'] = csrf
            session.update(user_id=user['id'], username=user['username'], name=user['name'])
            flash(f'Welcome back, {user["name"]}!', 'success')
            return redirect(safe_next(next_url))
        return render_template('auth.html', mode='login', error='Invalid username or password',
                               next=next_url, username=username, **_form_context()), 401
    return render_template('auth.html', mode='login', next=next_url, **_form_context())


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        username = (request.form.get('username') or '').strip().lower()
        password = (request.form.get('password') or '').strip()
        error = None
        if not name or not username or not password:
            error = 'Please fill in every field.'
        else:
            try:
                db().execute('INSERT INTO users (username, password_hash, name) VALUES (?, ?, ?)',
                             (username, generate_password_hash(password), name))
                db().commit()
            except sqlite3.IntegrityError:
                error = 'Username already exists'
        if error:
            return render_template('auth.html', mode='register', error=error, name=name,
                                   username=username, **_form_context()), 400
        flash('Registration successful! You can now log in.', 'success')
        return redirect(url_for('login'))
    return render_template('auth.html', mode='register', **_form_context())


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('index'))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def error_page(e):
    return render_template('error.html', error=e), e.code


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=os.environ.get('FLASK_DEBUG') == '1')

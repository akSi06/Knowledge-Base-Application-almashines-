from flask import Flask, render_template, request, jsonify
import requests
import os
from dotenv import load_dotenv
import logging
from datetime import datetime
from flask_mail import Mail, Message
from flask_sqlalchemy import SQLAlchemy 

load_dotenv()

app = Flask(__name__)

logging.basicConfig(
    filename='app.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False') == 'True'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

mail = Mail(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///cache.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

SORT_OPTION_MAPPING = {
    'relevance': 'relevance',
    'score': 'votes',
    'date': 'creation',
}

def fetch_stackoverflow(query, sort_option, page):
    stackoverflow_url = 'https://api.stackexchange.com/2.3/search/advanced'
    api_sort_option = SORT_OPTION_MAPPING.get(sort_option, 'relevance')
    stackoverflow_params = {
        'order': 'desc',
        'sort': api_sort_option,
        'q': query,
        'site': 'stackoverflow',
        'key': os.getenv('STACKOVERFLOW_KEY'),
        'page': page,
        'pagesize': 10
    }
    response = requests.get(stackoverflow_url, params=stackoverflow_params, timeout=10)
    response.raise_for_status()
    data = response.json()
    results = []
    for item in data.get('items', []):
        results.append({
            'source': 'Stack Overflow',
            'title': item.get('title', 'No Title'),
            'link': item.get('link', '#'),
            'is_answered': item.get('is_answered', False),
            'score': item.get('score', 0),
            'num_answers': item.get('answer_count', 0),
            'date': item.get('creation_date', 0)
        })
    return results

def fetch_reddit(query, sort_option, limit, after):
    reddit_url = 'https://www.reddit.com/search.json'
    reddit_headers = {
        'User-Agent': f"CodeQuestApp/1.0 by {os.getenv('REDDIT_USERNAME', 'yourusername')}"
    }
    reddit_params = {
        'q': query,
        'sort': sort_option if sort_option in ['relevance', 'new', 'hot', 'top'] else 'relevance',
        'limit': limit,
        'after': after
    }
    response = requests.get(reddit_url, headers=reddit_headers, params=reddit_params, timeout=10)
    response.raise_for_status()
    data = response.json()
    results = []
    new_after = data.get('data', {}).get('after', None)
    for item in data.get('data', {}).get('children', []):
        post = item['data']
        results.append({
            'source': 'Reddit',
            'title': post.get('title', 'No Title'),
            'link': f"https://www.reddit.com{post.get('permalink', '#')}",
            'is_answered': 'N/A',
            'score': post.get('score', 0),
            'num_comments': post.get('num_comments', 0),
            'date': post.get('created_utc', 0)
        })
    return results, new_after

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/send_email', methods=['POST'])
def send_email():
    data = request.get_json()
    if not data:
        logging.warning("No data received for email sending.")
        return jsonify({'error': 'No data provided'}), 400
    recipient_emails = data.get('recipients', [])
    if not recipient_emails:
        logging.warning("No recipient emails provided.")
        return jsonify({'error': 'No recipient emails provided'}), 400
    for email in recipient_emails:
        if not isinstance(email, str) or '@' not in email:
            logging.warning(f"Invalid email address detected: {email}")
            return jsonify({'error': f'Invalid email address: {email}'}), 400
    stackoverflow_results = data.get('stackoverflow', [])
    reddit_results = data.get('reddit', [])
    if not stackoverflow_results and not reddit_results:
        logging.warning("No search results to include in the email.")
        return jsonify({'error': 'No search results to include in the email.'}), 400
    email_body = render_template('email_template.html',
                                 stackoverflow=stackoverflow_results,
                                 reddit=reddit_results,
                                 query=data.get('query', 'N/A'),
                                 sort_option=data.get('sort_option', 'N/A'))
    try:
        msg = Message(subject=f'Code Quest Search Results for "{data.get("query", "")}"',
                      recipients=recipient_emails,
                      html=email_body)
        mail.send(msg)
        logging.info(f"Email sent successfully to {recipient_emails}")
        return jsonify({'message': 'Email sent successfully!'}), 200
    except Exception as e:
        logging.error(f"Failed to send email: {str(e)}")
        return jsonify({'error': 'Failed to send email. Please try again later.'}), 500
    
class SearchCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    query = db.Column(db.String(255), nullable=False, index=True)
    sort_option = db.Column(db.String(50), nullable=False)
    stackoverflow_results = db.Column(db.Text, nullable=True)
    reddit_results = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SearchCache query='{self.query}' sort='{self.sort_option}'>"

with app.app_context():
    db.create_all()

@app.template_filter('datetimeformat')
def datetimeformat(value, format='%Y-%m-%d %H:%M'):
    return datetime.utcfromtimestamp(value).strftime(format)

@app.route('/search', methods=['GET'])
def search():
    query = request.args.get('query', '').strip()
    sort_option = request.args.get('sort', 'relevance')
    page = int(request.args.get('page', 1))
    reddit_after = request.args.get('after', None)
    if not query:
        logging.warning("Search attempted without a query.")
        return jsonify({'error': 'No search query provided'}), 400
    stackoverflow_results = []
    reddit_results = []
    try:
        stackoverflow_results = fetch_stackoverflow(query, sort_option, page)
        reddit_limit = 10
        reddit_results, new_after = fetch_reddit(query, sort_option, reddit_limit, reddit_after)
        logging.info(f"Search completed. Query: '{query}', Sort: '{sort_option}', Page: {page}, "
                     f"Stack Overflow Results: {len(stackoverflow_results)}, Reddit Results: {len(reddit_results)}.")
    except requests.exceptions.RequestException as e:
        logging.error(f"API request failed for query '{query}' with error: {str(e)}.")
        return jsonify({'error': f'Error fetching data from APIs: {str(e)}'}), 500
    except Exception as e:
        logging.error(f"Unexpected error during search for query '{query}': {str(e)}.")
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500
    return jsonify({
        'stackoverflow': stackoverflow_results,
        'reddit': reddit_results,
        'has_more': len(reddit_results) == reddit_limit,
        'reddit_after': new_after
    })

if __name__ == '__main__':
    app.run(debug=True)

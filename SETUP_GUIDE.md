# Setup Guide for Enhanced Bot

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create or update your `.env` file with these additions:

```env
# Existing variables (keep these)
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHANNEL_ID=@your_channel
GROQ_API_KEY=your_key
NEWSDATA_API_KEY=your_key
MARKETAUX_API_KEY=your_key

# New: Admin token for remote commands
ADMIN_TOKEN=your-secure-random-token

# New: Webhook configuration (optional)
USE_WEBHOOK=false
WEBHOOK_URL=https://your-domain.com/webhook/telegram
WEBHOOK_PORT=5001
```

### 3. Run the Bot

```bash
python app.py
```

---

## New Features Guide

### Admin Commands

Use these commands to manage the bot remotely via Telegram:

#### Clear Cache
```
/admin cache_clear YOUR_ADMIN_TOKEN
```
Clears the context cache. Useful if memory usage is high.

#### Database Cleanup
```
/admin db_cleanup YOUR_ADMIN_TOKEN
```
Manually trigger database cleanup:
- Removes sent articles older than 30 days
- Prunes processed articles (keeps max 2000)
- Removes feedback older than 90 days

#### Queue Status
```
/admin queue_status YOUR_ADMIN_TOKEN
```
Check the Groq API request queue size. High values indicate the bot is busy.

#### Restart Bot
```
/admin restart YOUR_ADMIN_TOKEN
```
Gracefully restart the bot. Useful after configuration changes.

---

### Health Check Endpoint

Monitor bot health via HTTP:

```bash
curl http://localhost:5001/health
```

Response includes:
- Telegram connectivity
- Groq queue size
- Memory usage
- Thread status
- Metrics (uptime, posts, errors)
- Cache statistics
- Database info

---

### Webhook Mode (Optional)

For faster message delivery, enable webhook mode:

1. **Update `.env`:**
```env
USE_WEBHOOK=true
WEBHOOK_URL=https://your-domain.com/webhook/telegram
WEBHOOK_PORT=5001
```

2. **Ensure your domain points to the bot server**

3. **Restart the bot**

The bot will automatically:
- Remove old webhook
- Register new webhook
- Fall back to polling if webhook fails

---

### Database Cleanup

The bot automatically cleans the database:

- **Every 10 news cycles:** Removes sent articles >30 days old
- **Every 10 news cycles:** Prunes processed articles (keeps max 2000)
- **Every 50 news cycles:** Removes feedback >90 days old

You can also manually trigger cleanup via `/admin db_cleanup`.

---

### Monitoring

#### Via Health Endpoint
```bash
# Check bot status
curl http://localhost:5001/health | jq

# Check metrics only
curl http://localhost:5001/metrics | jq
```

#### Via Telegram
```
/stats
```
Shows comprehensive bot statistics including:
- Uptime
- Articles processed
- Posts sent
- AI processing stats
- Cache hit rate
- Processing rate

---

### Logging

Logs are written to:
- **Console:** Human-readable format (development)
- **File:** `bot.log` with JSON format (production)

Log files rotate automatically:
- Max size: 5MB per file
- Backup count: 5 files
- Total max: ~25MB

---

## Configuration Reference

### Timing (seconds)
```env
NEWS_CHECK_INTERVAL_SECONDS=1800        # How often to fetch news (default: 30 min)
TWEET_POST_DELAY_SECONDS=120            # Delay between posts (default: 2 min)
USER_RESET_TIMEOUT=300                  # User session timeout (default: 5 min)
GROQ_API_TIMEOUT=60                     # Groq API timeout (default: 60s)
QUESTION_API_TIMEOUT=45                 # User question timeout (default: 45s)
EXTERNAL_API_TIMEOUT=15                 # News API timeout (default: 15s)
IMAGE_VALIDATION_TIMEOUT=7              # Image check timeout (default: 7s)
```

### LLM Configuration
```env
GROQ_PRIMARY_MODEL=llama-3.3-70b-versatile
GROQ_FALLBACK_MODEL=llama-3.1-8b-instant
LLM_TEMPERATURE=0.2                     # Lower = more deterministic
LLM_TOP_P=1.0                           # Diversity parameter
LLM_MAX_TOKENS=8192                     # Max response length
GROQ_SERVICE_TIER=auto                  # 'on_demand', 'flex', or 'auto'
```

### News Keywords
```env
NEWS_KEYWORDS=Bitcoin, Crypto, Blockchain
```

---

## Troubleshooting

### High Memory Usage
```
/admin cache_clear YOUR_TOKEN
```
Then check `/health` to verify memory decreased.

### Queue Buildup
Check `/admin queue_status`. If consistently high:
- Increase `GROQ_API_TIMEOUT`
- Reduce `NEWS_CHECK_INTERVAL_SECONDS`
- Check Groq API status

### Telegram Posts Failing
Check logs for rate limiting (429 errors). The bot will retry automatically.

### Webhook Not Working
1. Verify domain is accessible from internet
2. Check firewall allows port 5001
3. Verify `WEBHOOK_URL` is correct
4. Check logs for webhook registration errors
5. Bot will fall back to polling automatically

### Database Growing Too Large
The bot cleans automatically, but you can manually trigger:
```
/admin db_cleanup YOUR_TOKEN
```

---

## Security Best Practices

1. **Admin Token:** Use a strong random token
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. **Environment Variables:** Never commit `.env` to git
   - Add `.env` to `.gitignore` (already done)
   - Use secrets management in production

3. **Webhook URL:** Use HTTPS only
   - Get SSL certificate (Let's Encrypt is free)
   - Verify domain ownership

4. **API Keys:** Rotate regularly
   - Groq API key
   - News API keys
   - Twitter credentials

---

## Performance Tuning

### For High Volume
```env
NEWS_CHECK_INTERVAL_SECONDS=900         # Check more frequently
TWEET_POST_DELAY_SECONDS=60             # Post faster
USE_WEBHOOK=true                        # Use webhook mode
```

### For Low Resource Usage
```env
NEWS_CHECK_INTERVAL_SECONDS=3600        # Check less frequently
TWEET_POST_DELAY_SECONDS=300            # Post slower
USE_WEBHOOK=false                       # Use polling
```

---

## Deployment

### Docker
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app.py .
CMD ["python", "app.py"]
```

### Railway (Current Setup)
- Procfile already configured
- Environment variables set in Railway dashboard
- Logs available in Railway console

### Heroku
```bash
heroku create your-app-name
heroku config:set TELEGRAM_BOT_TOKEN=...
git push heroku main
```

---

## Support

For issues or questions:
1. Check logs: `tail -f bot.log`
2. Check health: `curl http://localhost:5001/health`
3. Check stats: `/stats` in Telegram
4. Review IMPROVEMENTS.md for feature details

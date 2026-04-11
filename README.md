The RBI Circulars Bot is a Python-based automation tool designed to monitor the Reserve Bank of India (RBI) website for new notifications and circulars. It streamlines the process of staying updated with regulatory changes by automatically fetching updates and broadcasting them to a Telegram channel.

Key Features
- Periodically scrapes the RBI website to detect newly published documents.

- Sends alerts directly to a designated Telegram channel or bot interface, ensuring real-time delivery of regulatory information.

- Utilizes a local database (SQLite) to track previously processed circulars, preventing duplicate notifications.

- Capable of capturing titles and direct links to the official PDF documents for quick access.

Technical Implementation
The project is built using a standard Python stack focused on web scraping and API integration:

- Employs libraries such as BeautifulSoup and requests to parse the HTML structure of the RBI notifications page.

- Uses the python-telegram-bot library (or direct API requests) to interface with the Telegram Bot API for message delivery.

- Uses SQLite to store unique identifiers for each circular, acting as a simple state-management system.

- Utilizes environment variables (via .env files) to securely manage sensitive data like API tokens and chat IDs.

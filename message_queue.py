"""
Message queue system with rate limiting to avoid saturating Telegram.
Implements:
- Message queue with configurable delays
- Retries, only when the message certainly was not delivered
- Rate limiting error handling (honours Telegram's retry_after)
"""

import queue
import re
import time
from threading import Thread, Lock

from requests.exceptions import ConnectionError, ConnectTimeout
from telebot.apihelper import ApiTelegramException
from urllib3.exceptions import MaxRetryError, NewConnectionError

from logger import debug, error, warning


def _retry_after(e):
	"""Seconds Telegram asks to wait when it rejected the request for flooding
	(429), or None for any other failure"""
	if isinstance(e, ApiTelegramException) and e.error_code == 429:
		try:
			return int(e.result_json["parameters"]["retry_after"])
		except (KeyError, TypeError, ValueError):
			return 2
	return None


def _describe(e):
	"""Error text for the log, without the bot token that request URLs carry"""
	return re.sub(r"/bot[^/\s]+/", "/bot<token>/", str(e))


def _not_delivered(e):
	"""True when the request certainly never reached Telegram: the connection
	could not be established. A read timeout or a connection dropped mid-request
	may come after Telegram already sent the message"""
	if isinstance(e, ConnectTimeout):
		return True
	if isinstance(e, ConnectionError) and e.args and isinstance(e.args[0], MaxRetryError):
		return isinstance(e.args[0].reason, NewConnectionError)
	return False


class MessageQueue:
	def __init__(self, delay_between_messages=0.5, max_retries=3):
		self.queue = queue.Queue()
		self.delay_between_messages = delay_between_messages
		self.max_retries = max_retries
		self.lock = Lock()
		self.running = True
		self.worker_thread = Thread(target=self._process_queue, daemon=True)
		self.worker_thread.start()
		debug("Message queue started")

	def _process_queue(self):
		"""Continuously processes the message queue"""
		while self.running:
			try:
				# Get the next message from the queue (timeout to allow shutdown)
				message_data = self.queue.get(timeout=1)
				if message_data is None:  # Stop signal
					break

				self._execute_message(message_data)
				time.sleep(self.delay_between_messages)
			except queue.Empty:
				continue
			except Exception as e:
				error(f"Error processing message queue: {str(e)}")

	def _execute_message(self, message_data):
		"""Executes a message with retries and exponential backoff. Only failures
		where Telegram certainly did not act on the request are retried: sending
		is not idempotent, so retrying a request that may have been delivered
		(a response that timed out or was cut off) duplicates the message"""
		func = message_data['func']
		args = message_data['args']
		kwargs = message_data['kwargs']
		result_queue = message_data.get('result_queue')
		result = None

		try:
			for attempt in range(self.max_retries):
				try:
					result = func(*args, **kwargs)
					break
				except Exception as e:
					retry_after = _retry_after(e)
					last_attempt = attempt == self.max_retries - 1
					if retry_after is not None and not last_attempt:
						warning(f"Rate limit detected. Waiting {retry_after}s before retrying...")
						time.sleep(retry_after)
					elif _not_delivered(e) and not last_attempt:
						wait_time = 1 * (attempt + 1)
						debug(f"Error sending message (attempt {attempt + 1}/{self.max_retries}): {_describe(e)}. Retrying in {wait_time}s...")
						time.sleep(wait_time)
					elif retry_after is not None or _not_delivered(e):
						error(f"Final error sending message after {self.max_retries} attempts: {_describe(e)}")
						break
					elif isinstance(e, ApiTelegramException):
						error(f"Error sending message: {_describe(e)}")
						break
					else:
						warning(f"Error sending message, not retried because it may have been delivered: {_describe(e)}")
						break
		except Exception as e:
			error(f"Error processing message queue: {str(e)}")
		finally:
			if result_queue:
				result_queue.put(result)
		return result

	def enqueue(self, func, *args, **kwargs):
		"""Adds a message to the queue (fire and forget)"""
		self.queue.put({'func': func, 'args': args, 'kwargs': kwargs})

	def enqueue_and_wait(self, func, *args, timeout=120, **kwargs):
		"""Adds a message to the queue and waits for the result. The timeout covers
		the messages ahead in the queue plus every attempt of this one, so it must
		stay well above telebot's own read timeout (30s)"""
		result_queue = queue.Queue()
		self.queue.put({'func': func, 'args': args, 'kwargs': kwargs, 'result_queue': result_queue})
		try:
			return result_queue.get(timeout=timeout)
		except queue.Empty:
			error("Timeout waiting for message queue result")
			return None

	def stop(self):
		"""Stops the message queue"""
		self.running = False
		self.queue.put(None)

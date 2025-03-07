# A part of NonVisual Desktop Access (NVDA)
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.
# Copyright (C) 2006-2025 NV Access Limited, Davy Kager, Julien Cochuyt, Rob Meredith, Leonard de Ruijter

"""Common support for editable text.
@note: If you want editable text functionality for an NVDAObject,
	you should use the EditableText classes in L{NVDAObjects.behaviors}.
"""

import time
from numbers import Real
from speech import sayAll
import api
import review
from baseObject import ScriptableObject
from documentBase import TextContainerObject
import braille
import speech
import config
import eventHandler
from scriptHandler import isScriptWaiting, willSayAllResume
import textInfos
import controlTypes
from inputCore import InputGesture
from logHandler import log
from comtypes import COMError


class EditableText(TextContainerObject, ScriptableObject):
	"""Provides scripts to report appropriately when moving the caret in editable text fields.
	This does not handle the selection change keys.
	To have selection changes reported, the object must notify of selection changes.
	If the object supports selection but does not notify of selection changes, L{EditableTextWithoutAutoSelectDetection} should be used instead.

	If the object notifies of selection changes, the following should be done:
		* When the object gains focus, L{initAutoSelectDetection} must be called.
		* When the object notifies of a possible selection change, L{detectPossibleSelectionChange} must be called.
		* Optionally, if the object notifies of changes to its content, L{hasContentChangedSinceLastSelection} should be set to C{True}.
	"""

	hasContentChangedSinceLastSelection: bool
	"""Whether the content has changed since the last selection occurred."""

	shouldFireCaretMovementFailedEvents: bool = False
	"""Whether to fire caretMovementFailed events when the caret doesn't move in response to a caret movement key."""

	announceNewLineText: bool = True
	"""Whether or not to announce text found before the caret on a new line (e.g. auto numbering)"""

	announceEntireNewLine: bool = False
	"""When announcing new line text: should the entire line be announced, or just text after the caret?"""

	_hasCaretMoved_minWordTimeoutSec: float = 0.03
	"""The minimum amount of time that should elapse before checking if the word under the caret has changed"""

	_useEvents_maxTimeoutSec: float = 0.06
	"""The maximum amount of time that may elapse before we no longer rely on caret events to detect movement."""

	_caretMovementTimeoutMultiplier: Real = 1
	"""A multiplier to apply to the caret movement timeout to increase or decrease it in a subclass."""

	_supportsSentenceNavigation: bool | None = None
	"""Whether the editable text supports sentence navigation.
	When `None` (default), the state is undetermined, e.g. sentence navigation will be attempted, when it fails, the gesture will be send to the OS.
	When `True`, sentence navigation is explicitly supported and will be performed. When it fails, the gesture is discarded.
	When `False`, sentence navigation is explicitly not supported and the gesture is sent to the OS.
	"""

	def _hasCaretMoved(self, bookmark, retryInterval=0.01, timeout=None, origWord=None):
		"""
		Waits for the caret to move, for a timeout to elapse, or for a new focus event or script to be queued.
		@param bookmark: a bookmark representing the position of the caret before  it was instructed to move
		@type bookmark: bookmark
		@param retryInterval: the interval of time in seconds this method should  wait before checking the caret each time.
		@type retryInterval: float
		@param timeout: the over all amount of time in seconds the method should wait before giving up completely,
			C{None} to use the value from the configuration.
		@type timeout: float
		@param origWord: The word at the caret before the movement command,
			C{None} if the word at the caret should not be used to detect movement.
			This is intended for use with the delete key.
		@return: a tuple containing a boolean denoting whether this method timed out, and  a TextInfo representing the old or updated caret position or None if interupted by a script or focus event.
		@rtype: tuple
		"""
		if timeout is None:
			timeout = config.conf["editableText"]["caretMoveTimeoutMs"] / 1000
		timeout *= self._caretMovementTimeoutMultiplier
		start = time.time()
		elapsed = 0
		newInfo = None
		retries = 0
		while True:
			if isScriptWaiting():
				return (False, None)
			api.processPendingEvents(processEventQueue=False)
			if eventHandler.isPendingEvents("gainFocus"):
				log.debug("Focus event. Elapsed %g sec" % elapsed)
				return (True, None)
			# Caret events are unreliable in some controls.
			# Only use them if we consider them safe to rely on for a particular control,
			# and only if they arrive within C{_useEvents_maxTimeoutSec} seconds
			# after causing the event to occur.
			if (
				elapsed <= self._useEvents_maxTimeoutSec
				and self.caretMovementDetectionUsesEvents
				and (eventHandler.isPendingEvents("caret") or eventHandler.isPendingEvents("textChange"))
			):
				log.debug(
					"Caret move detected using event. Elapsed %g sec, retries %d" % (elapsed, retries),
				)
				# We must fetch the caret here rather than above the isPendingEvents check
				# to avoid a race condition where an event is queued from a background
				# thread just after we query the caret. In that case, the caret info we
				# retrieved might be stale.
				try:
					newInfo = self.makeTextInfo(textInfos.POSITION_CARET)
				except (RuntimeError, NotImplementedError):
					newInfo = None
				return (True, newInfo)
			# If the focus changes after this point, fetching the caret may fail,
			# but we still want to stay in this loop.
			try:
				newInfo = self.makeTextInfo(textInfos.POSITION_CARET)
			except (RuntimeError, NotImplementedError):
				newInfo = None
			# Try to detect with bookmarks.
			newBookmark = None
			if newInfo:
				try:
					newBookmark = newInfo.bookmark
				except (RuntimeError, NotImplementedError):
					pass
			if newBookmark and newBookmark != bookmark:
				log.debug(
					"Caret move detected using bookmarks. Elapsed %g sec, retries %d" % (elapsed, retries),
				)
				return (True, newInfo)
			if origWord is not None and newInfo and elapsed >= self._hasCaretMoved_minWordTimeoutSec:
				# When pressing delete, bookmarks might not be enough to detect caret movement.
				# Therefore try detecting if the word under the caret has changed, such as when pressing delete.
				# some editors such as Mozilla Gecko can have text and units that get out of sync with eachother while a character is being deleted.
				# Therefore, only check if the word has changed after a particular amount of time has elapsed, allowing the text and units to settle down.
				wordInfo = newInfo.copy()
				wordInfo.expand(textInfos.UNIT_WORD)
				word = wordInfo.text
				if word != origWord:
					log.debug("Word at caret changed. Elapsed: %g sec" % elapsed)
					return (True, newInfo)
			elapsed = time.time() - start
			if elapsed >= timeout:
				break
			# We spin the first few tries, as sleep is not accurate for tiny periods
			# and we might end up sleeping longer than we need to. Spinning improves
			# responsiveness in the case that the app responds fairly quickly.
			if retries > 2:
				# Don't spin too long, though. If we get to this point, the app is
				# probably taking a while to respond, so super fast response is
				# already lost.
				time.sleep(retryInterval)
			retries += 1
		log.debug("Caret didn't move before timeout. Elapsed: %g sec" % elapsed)
		return (False, newInfo)

	def _caretScriptPostMovedHelper(self, speakUnit, gesture, info=None):
		if isScriptWaiting():
			return
		if not info:
			try:
				info = self.makeTextInfo(textInfos.POSITION_CARET)
			except:  # noqa: E722
				return
		# Forget the word currently being typed as the user has moved the caret somewhere else.
		speech.clearTypedWordBuffer()
		review.handleCaretMove(info)
		if speakUnit and not willSayAllResume(gesture):
			info.expand(speakUnit)
			speech.speakTextInfo(info, unit=speakUnit, reason=controlTypes.OutputReason.CARET)
		braille.handler.handleCaretMove(self)

	def _caretMovementScriptHelper(self, gesture, unit):
		try:
			info = self.makeTextInfo(textInfos.POSITION_CARET)
		except:  # noqa: E722
			gesture.send()
			return
		bookmark = info.bookmark
		gesture.send()
		caretMoved, newInfo = self._hasCaretMoved(bookmark)
		if not caretMoved and self.shouldFireCaretMovementFailedEvents:
			eventHandler.executeEvent("caretMovementFailed", self, gesture=gesture)
		self._caretScriptPostMovedHelper(unit, gesture, newInfo)

	def _get_caretMovementDetectionUsesEvents(self) -> bool:
		"""Returns whether or not to rely on caret and textChange events when
		finding out whether the caret position has changed after pressing a caret movement gesture.
		Note that if L{_useEvents_maxTimeoutMs} is elapsed,
		relying on events is no longer reliable in most situations.
		Therefore, any event should occur before that timeout elapses.
		"""
		# This class is a mixin that usually comes before other relevant classes in the mro.
		# Therefore, try to call super first, and if that fails, return the default (C{True}.
		try:
			return super().caretMovementDetectionUsesEvents
		except AttributeError:
			return True

	def script_caret_newLine(self, gesture):
		try:
			info = self.makeTextInfo(textInfos.POSITION_CARET)
		except:  # noqa: E722
			gesture.send()
			return
		bookmark = info.bookmark
		gesture.send()
		caretMoved, newInfo = self._hasCaretMoved(bookmark)
		if not caretMoved or not newInfo:
			return
		# newInfo.copy should be good enough here, but in MS Word we get strange results.
		try:
			lineInfo = self.makeTextInfo(textInfos.POSITION_CARET)
		except (RuntimeError, NotImplementedError):
			return
		lineInfo.expand(textInfos.UNIT_LINE)
		if not self.announceEntireNewLine:
			lineInfo.setEndPoint(newInfo, "endToStart")
		if lineInfo.isCollapsed:
			lineInfo.expand(textInfos.UNIT_CHARACTER)
			onlyInitial = True
		else:
			onlyInitial = False
		speech.speakTextInfo(
			lineInfo,
			unit=textInfos.UNIT_LINE,
			reason=controlTypes.OutputReason.CARET,
			onlyInitialFields=onlyInitial,
			suppressBlanks=True,
		)

	def _caretMoveBySentenceHelper(self, gesture: InputGesture, direction: int) -> None:
		if isScriptWaiting():
			if not self._supportsSentenceNavigation:  # either None or False
				gesture.send()
			return
		try:
			info = self.makeTextInfo(textInfos.POSITION_CARET)
			caretMoved = False
			newInfo = None
			if not self._supportsSentenceNavigation:
				bookmark = info.bookmark
				gesture.send()
				caretMoved, newInfo = self._hasCaretMoved(bookmark)
			if not caretMoved and self._supportsSentenceNavigation is not False:
				info.move(textInfos.UNIT_SENTENCE, direction)
				info.updateCaret()
			else:
				info = newInfo
			self._caretScriptPostMovedHelper(
				textInfos.UNIT_SENTENCE if not caretMoved else textInfos.UNIT_LINE,
				gesture,
				info,
			)
		except Exception:
			if self._supportsSentenceNavigation is True:
				log.exception("Error in _caretMoveBySentenceHelper")

	def script_caret_moveByLine(self, gesture):
		self._caretMovementScriptHelper(gesture, textInfos.UNIT_LINE)

	script_caret_moveByLine.resumeSayAllMode = sayAll.CURSOR.CARET

	def script_caret_moveByCharacter(self, gesture):
		self._caretMovementScriptHelper(gesture, textInfos.UNIT_CHARACTER)

	def script_caret_moveByWord(self, gesture):
		self._caretMovementScriptHelper(gesture, textInfos.UNIT_WORD)

	def script_caret_moveByParagraph(self, gesture):
		self._caretMovementScriptHelper(gesture, textInfos.UNIT_PARAGRAPH)

	script_caret_moveByParagraph.resumeSayAllMode = sayAll.CURSOR.CARET

	def script_caret_previousSentence(self, gesture):
		self._caretMoveBySentenceHelper(gesture, -1)

	script_caret_previousSentence.resumeSayAllMode = sayAll.CURSOR.CARET

	def script_caret_nextSentence(self, gesture):
		self._caretMoveBySentenceHelper(gesture, 1)

	script_caret_nextSentence.resumeSayAllMode = sayAll.CURSOR.CARET

	def _backspaceScriptHelper(self, unit, gesture):
		try:
			oldInfo = self.makeTextInfo(textInfos.POSITION_CARET)
		except:  # noqa: E722
			gesture.send()
			return
		oldBookmark = oldInfo.bookmark
		testInfo = oldInfo.copy()
		try:
			res = testInfo.move(textInfos.UNIT_CHARACTER, -1)
		except COMError:
			log.exception("Error in testInfo.move")
			gesture.send()
			return
		if res < 0:
			testInfo.expand(unit)
			delChunk = testInfo.text
		else:
			delChunk = ""
		gesture.send()
		caretMoved, newInfo = self._hasCaretMoved(oldBookmark)
		if not caretMoved:
			return
		delChunk = delChunk.replace("\r\n", "\n")  # Occurs with at least with Scintilla
		if len(delChunk) > 1:
			speech.speakMessage(delChunk)
		else:
			speech.speakSpelling(delChunk)
		self._caretScriptPostMovedHelper(None, gesture, newInfo)

	def script_caret_backspaceCharacter(self, gesture):
		self._backspaceScriptHelper(textInfos.UNIT_CHARACTER, gesture)

	def script_caret_backspaceWord(self, gesture):
		self._backspaceScriptHelper(textInfos.UNIT_WORD, gesture)

	def _deleteScriptHelper(self, unit, gesture):
		try:
			info = self.makeTextInfo(textInfos.POSITION_CARET)
		except:  # noqa: E722
			gesture.send()
			return
		bookmark = info.bookmark
		info.expand(textInfos.UNIT_WORD)
		word = info.text
		gesture.send()
		# We'll try waiting for the caret to move, but we don't care if it doesn't.
		caretMoved, newInfo = self._hasCaretMoved(bookmark, origWord=word)
		self._caretScriptPostMovedHelper(unit, gesture, newInfo)
		braille.handler.handleCaretMove(self)

	def script_caret_deleteCharacter(self, gesture):
		self._deleteScriptHelper(textInfos.UNIT_CHARACTER, gesture)

	def script_caret_deleteWord(self, gesture):
		self._deleteScriptHelper(textInfos.UNIT_WORD, gesture)

	def _handleParagraphNavigation(self, gesture: InputGesture, nextParagraph: bool) -> None:
		from config.featureFlagEnums import ParagraphNavigationFlag

		flag: config.featureFlag.FeatureFlag = config.conf["documentNavigation"]["paragraphStyle"]
		if flag.calculated() == ParagraphNavigationFlag.APPLICATION:
			self.script_caret_moveByParagraph(gesture)
		elif flag.calculated() == ParagraphNavigationFlag.SINGLE_LINE_BREAK:
			from documentNavigation.paragraphHelper import moveToSingleLineBreakParagraph

			passKey, moved = moveToSingleLineBreakParagraph(
				nextParagraph=nextParagraph,
				speakNew=not willSayAllResume(gesture),
			)
			if passKey:
				self.script_caret_moveByParagraph(gesture)
		elif flag.calculated() == ParagraphNavigationFlag.MULTI_LINE_BREAK:
			from documentNavigation.paragraphHelper import moveToMultiLineBreakParagraph

			passKey, moved = moveToMultiLineBreakParagraph(
				nextParagraph=nextParagraph,
				speakNew=not willSayAllResume(gesture),
			)
			if passKey:
				self.script_caret_moveByParagraph(gesture)
		else:
			log.error(f"Unexpected ParagraphNavigationFlag value {flag.value}")

	def script_caret_previousParagraph(self, gesture: InputGesture) -> None:
		self._handleParagraphNavigation(gesture, False)

	script_caret_previousParagraph.resumeSayAllMode = sayAll.CURSOR.CARET

	def script_caret_nextParagraph(self, gesture: InputGesture) -> None:
		self._handleParagraphNavigation(gesture, True)

	script_caret_nextParagraph.resumeSayAllMode = sayAll.CURSOR.CARET

	__gestures = {
		"kb:upArrow": "caret_moveByLine",
		"kb:downArrow": "caret_moveByLine",
		"kb:leftArrow": "caret_moveByCharacter",
		"kb:rightArrow": "caret_moveByCharacter",
		"kb:pageUp": "caret_moveByLine",
		"kb:pageDown": "caret_moveByLine",
		"kb:control+leftArrow": "caret_moveByWord",
		"kb:control+rightArrow": "caret_moveByWord",
		"kb:control+upArrow": "caret_previousParagraph",
		"kb:control+downArrow": "caret_nextParagraph",
		"kb:alt+upArrow": "caret_previousSentence",
		"kb:alt+downArrow": "caret_nextSentence",
		"kb:home": "caret_moveByCharacter",
		"kb:end": "caret_moveByCharacter",
		"kb:control+home": "caret_moveByLine",
		"kb:control+end": "caret_moveByLine",
		"kb:delete": "caret_deleteCharacter",
		"kb:shift+delete": "caret_deleteCharacter",
		"kb:numpadDelete": "caret_deleteCharacter",
		"kb:shift+numpadDelete": "caret_deleteCharacter",
		"kb:control+delete": "caret_deleteWord",
		"kb:control+numpadDelete": "caret_deleteWord",
		"kb:backspace": "caret_backspaceCharacter",
		"kb:shift+backspace": "caret_backspaceCharacter",
		"kb:control+backspace": "caret_backspaceWord",
	}

	_autoSelectDetectionEnabled = False

	def initAutoSelectDetection(self):
		"""Initialise automatic detection of selection changes.
		This should be called when the object gains focus.
		"""
		try:
			self._lastSelectionPos = self.makeTextInfo(textInfos.POSITION_SELECTION)
		except:  # noqa: E722
			self._lastSelectionPos = None
		self.isTextSelectionAnchoredAtStart = True
		self.hasContentChangedSinceLastSelection = False
		self._autoSelectDetectionEnabled = True

	def detectPossibleSelectionChange(self):
		"""Detects if the selection has been changed, and if so it speaks the change."""
		if not self._autoSelectDetectionEnabled:
			return
		try:
			newInfo = self.makeTextInfo(textInfos.POSITION_SELECTION)
		except:  # noqa: E722
			# Just leave the old selection, which is usually better than nothing.
			return
		oldInfo = getattr(self, "_lastSelectionPos", None)
		self._lastSelectionPos = newInfo.copy()
		if not oldInfo:
			# There's nothing we can do, but at least the last selection will be right next time.
			self.isTextSelectionAnchoredAtStart = True
			return
		try:
			self._updateSelectionAnchor(oldInfo, newInfo)
		except COMError:
			log.exception("Error in _updateSelectionAnchor")
			return
		hasContentChanged = getattr(self, "hasContentChangedSinceLastSelection", False)
		self.hasContentChangedSinceLastSelection = False
		speech.speakSelectionChange(oldInfo, newInfo, generalize=hasContentChanged)

	def _updateSelectionAnchor(self, oldInfo, newInfo):
		# Only update the value if the selection changed.
		if newInfo.compareEndPoints(oldInfo, "startToStart") != 0:
			self.isTextSelectionAnchoredAtStart = False
		elif newInfo.compareEndPoints(oldInfo, "endToEnd") != 0:
			self.isTextSelectionAnchoredAtStart = True

	def terminateAutoSelectDetection(self):
		"""Terminate automatic detection of selection changes.
		This should be called when the object loses focus.
		"""
		self._lastSelectionPos = None
		self._autoSelectDetectionEnabled = False


class EditableTextWithoutAutoSelectDetection(EditableText):
	"""In addition to L{EditableText}, provides scripts to report appropriately when the selection changes.
	This should be used when an object does not notify of selection changes.
	"""

	def reportSelectionChange(self, oldTextInfo):
		api.processPendingEvents(processEventQueue=False)
		newInfo = self.makeTextInfo(textInfos.POSITION_SELECTION)
		self._updateSelectionAnchor(oldTextInfo, newInfo)
		speech.speakSelectionChange(oldTextInfo, newInfo)
		braille.handler.handleCaretMove(self)

	def script_caret_changeSelection(self, gesture):
		try:
			oldInfo = self.makeTextInfo(textInfos.POSITION_SELECTION)
		except:  # noqa: E722
			gesture.send()
			return
		gesture.send()
		if isScriptWaiting() or eventHandler.isPendingEvents("gainFocus"):
			return
		try:
			self.reportSelectionChange(oldInfo)
		except:  # noqa: E722
			return

	__changeSelectionGestures = (
		"kb:shift+upArrow",
		"kb:shift+downArrow",
		"kb:shift+leftArrow",
		"kb:shift+rightArrow",
		"kb:shift+pageUp",
		"kb:shift+pageDown",
		"kb:shift+control+leftArrow",
		"kb:shift+control+rightArrow",
		"kb:shift+control+upArrow",
		"kb:shift+control+downArrow",
		"kb:shift+home",
		"kb:shift+end",
		"kb:shift+control+home",
		"kb:shift+control+end",
		"kb:control+a",
	)

	def initClass(self):
		for gesture in self.__changeSelectionGestures:
			self.bindGesture(gesture, "caret_changeSelection")

# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2021 NV Access Limited
# This file may be used under the terms of the GNU General Public License, version 2 or later.
# For more details see: https://www.gnu.org/licenses/gpl-2.0.html
*** Settings ***
Documentation	Tests how NVDA interacts with various features of the Windows system
Force Tags	NVDA	smoke test

# for start & quit in Test Setup and Test Test Teardown
Library	NvdaLib.py
Library	windowsTests.py
Library	ScreenCapLibrary

Test Setup	start NVDA	standard-dontShowWelcomeDialog.ini
Test Teardown	default teardown

*** Keywords ***
default teardown
	${screenshotName}=	create_preserved_test_output_filename	failedTest.png
	Run Keyword If Test Failed	Take Screenshot	${screenShotName}
	quit NVDA

setup and open windows search
	start NVDA	standard-dontShowWelcomeDialog.ini
	open windows search

close windows search and teardown
	${screenshotName}=	create_preserved_test_output_filename	failedTest.png
	Run Keyword If Test Failed	Take Screenshot	${screenShotName}
	close windows search
	quit NVDA

*** Test Cases ***
emoji panel search
	[Documentation]	Read emoji by navigating the emoji panel
	[Setup]	setup and open windows search
	[Teardown]	close windows search and teardown
	[Tags]	excluded_from_build	# AppVeyor's Windows build doesn't have an emoji panel with searching
	open emoji panel
	search emojis	came
	read emojis	camel	two-hump camel	camera


emoji panel open
	[Documentation]	Confirm that opening the emoji panel announces an emoji
	[Setup]	setup and open windows search
	[Teardown]	close windows search and teardown
	${firstEmoji}=	open emoji panel	# set expected first emoji
	search emojis	${firstEmoji}
	read emojis	${firstEmoji}


clipboard history
	[Documentation]	Copy text and read from the clipboard history
	[Setup]	setup and open windows search
	[Teardown]	close windows search and teardown
	write and copy text	foo
	write and copy text	lorem ipsum
	write and copy text	bar
	open clipboard history
	read clipboard history	bar	lorem ipsum	foo


toggle between emoji panel and clipboard history
	[Documentation]	Toggle between clipboard history and emoji panel and ensure items are announced
	[Setup]	setup and open windows search
	[Teardown]	close windows search and teardown
	write and copy text	test toggle between
	${firstEmoji}=	open emoji panel
	open clipboard history
	read clipboard history	test toggle between
	open emoji panel
	read emojis	${firstEmoji}

-- Interviews — ouvre l'application de transcription.
-- Au premier lancement, installe ce qu'il faut en affichant une fenêtre de progression,
-- puis démarre le serveur local et ouvre la page dans le navigateur.

property engine : ""

on run
	try
		set engine to quoted form of (POSIX path of (path to resource "Interviews.command"))
	on error
		display alert "Interviews est incomplet" message "Retéléchargez l'application." as critical
		return
	end try
	
	-- Déjà en route ? on rouvre simplement la page.
	set runningPort to runEngine(" port")
	if runningPort is not "" then
		openPage(runningPort)
		return
	end if
	
	try
		set state to runEngine(" check")
	on error problemText
		display alert "Interviews ne peut pas fonctionner sur ce Mac" message problemText as critical
		return
	end try
	
	if state is "need-install" then
		if not installEverything() then return
	end if
	
	set progress total steps to -1
	set progress completed steps to 0
	set progress description to "Démarrage d'Interviews…"
	set progress additional description to ""
	try
		runEngine(" serve")
	on error errorText
		set progress total steps to 0
		display alert "Interviews n'a pas pu démarrer" message errorText as critical
		return
	end try
	
	set thePort to ""
	repeat 240 times
		set thePort to runEngine(" port")
		if thePort is not "" then exit repeat
		delay 0.5
	end repeat
	set progress total steps to 0
	
	if thePort is "" then
		offerLog("Interviews n'a pas réussi à démarrer.", "~/.qwen-transcribe/server.log")
		return
	end if
	openPage(thePort)
end run

on installEverything()
	set progress total steps to -1
	set progress completed steps to 0
	set progress description to "Installation d'Interviews"
	set progress additional description to "Premier lancement : environ 7 Go à télécharger, de 15 à 45 minutes."
	try
		runEngine(" install-start")
	on error errorText
		set progress total steps to 0
		display alert "L'installation n'a pas pu démarrer" message errorText as critical
		return false
	end try
	repeat
		delay 2
		set state to runEngine(" install-state")
		if state is "done" then
			set progress total steps to 0
			return true
		else if state starts with "error|" then
			set progress total steps to 0
			offerLog(field(state, 2), "~/.qwen-transcribe/install.log")
			return false
		else if state starts with "running|" then
			set progress description to "Installation d'Interviews — étape " & field(state, 2)
			set progress additional description to field(state, 3)
		end if
	end repeat
end installEverything

on openPage(thePort)
	open location "http://127.0.0.1:" & thePort & "/"
end openPage

on offerLog(messageText, logPath)
	display alert "Interviews" message messageText & return & return & "Vous pouvez réessayer en rouvrant l'application." buttons {"Voir le détail", "OK"} default button "OK" as critical
	if button returned of result is "Voir le détail" then
		do shell script "open -a TextEdit " & logPath
	end if
end offerLog

on runEngine(arguments)
	with timeout of 7200 seconds
		return do shell script engine & arguments
	end timeout
end runEngine

on field(theText, position)
	set saved to AppleScript's text item delimiters
	set AppleScript's text item delimiters to "|"
	set parts to text items of theText
	set AppleScript's text item delimiters to saved
	if position > (count of parts) then return ""
	return item position of parts
end field


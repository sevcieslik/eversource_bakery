@echo off

:Set Inputs

SET /p Version="What Version?: "

:Generate Dates

for /f "skip=1" %%x in ('wmic os get localdatetime') do if not defined MyDate set MyDate=%%x
for /f %%x in ('wmic path win32_localtime get /format:list ^| findstr "="') do set %%x
set fmonth=00%Month%
set fday=00%Day%
set today=%Year%%fmonth:~-2%%fday:~-2%

:Make Output Folder

MD "%today% Version %Version%"
set outputfolder="%FolderLocation%\%today% Version %Version%"

:Las Header Info

las2las -i *.las -epsg 6492 -survey_feet -elevation_survey_feet -vertical_navd88 -odir "%today% Version %Version%" -olas -cores 6
pause

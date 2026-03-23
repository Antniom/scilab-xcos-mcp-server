// Scilab script to start the XcosAI polling loop
// This script is intended to be run from the scilab-xcos-mcp-server directory
chdir(get_absolute_file_path("start_poll.sce"));
exec("data/xcosai_poll_loop.sci", -1);
xcosai_poll_loop();
exit();

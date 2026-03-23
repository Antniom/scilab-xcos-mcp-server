function block=xcosai_nop_sim(block, flag)
    // No-op simulation function for graphical blocks during headless validation
endfunction


function xcosai_poll_loop()
    // xcosai_poll_loop.sci
    // Polls the Python server for new Xcos diagrams to validate.
    
    global XCOSAI_SERVER_PORT;
    if ~isdef('XCOSAI_SERVER_PORT') | isempty(XCOSAI_SERVER_PORT) then 
        XCOSAI_SERVER_PORT = 8000; 
    end
    
    POLL_MS = 1000;
    
    try
        rand('seed', getdate('s'));
    catch
    end
    
    LoopID = floor(rand()*10000);
    sleep(floor(rand()*500));
    
    disp('[XcosAI][' + string(LoopID) + '] Starting poll loop on port ' + string(XCOSAI_SERVER_PORT) + '...');
    disp('[XcosAI][' + string(LoopID) + '] Press Ctrl+C in Scilab console to stop.');
    
    global XCOSAI_POLLING_ACTIVE;
    XCOSAI_POLLING_ACTIVE = %t;
    
    url_base = 'http://127.0.0.1:' + string(XCOSAI_SERVER_PORT);
    startup_phase_end = getdate('s') + 10;
    
    while XCOSAI_POLLING_ACTIVE
        try
            resp = [];
            status = 0;
            try
                [resp, status] = http_get(url_base + '/task?loop_id=' + string(LoopID));
            catch
                if getdate('s') < startup_phase_end then
                    // Silently wait during startup
                else
                    [err_msg, err_id] = lasterror();
                    disp('[XcosAI][' + string(LoopID) + '] Connection Error [' + string(err_id) + ']: ' + string(err_msg));
                    disp('[XcosAI][' + string(LoopID) + '] Retrying in 5s...');
                    sleep(4000);
                end
                sleep(1000);
                continue;
            end
            
            if status == 200 then
                if isempty(resp) then continue; end
                
                task = [];
                if isstruct(resp) then
                    task = resp;
                else
                    try
                        task = fromJSON(resp);
                    catch
                        disp('[XcosAI][' + string(LoopID) + '] JSON Parse Fail: ' + string(resp));
                        continue;
                    end
                end
                
                if isstruct(task) & isfield(task, 'status') then
                    if task.status == 'busy' then
                        sleep(2000);
                        continue;
                    end

                    if task.status == 'pending' then
                        disp('[XcosAI][' + string(LoopID) + '] TASK RECEIVED: ' + string(task.task_id));
                        
                        task_id = ''; zcos_path = '';
                        try
                            task_id = task.task_id;
                            zcos_path = task.zcos_path;
                        catch
                            disp('[XcosAI][' + string(LoopID) + '] Error accessing task fields.');
                            continue;
                        end
                        
                        disp('[XcosAI][' + string(LoopID) + '] Target path: ' + string(zcos_path));
                    
                        success = %f;
                        err_msg = '';
                        
                        diary_path = zcos_path + '.log';
                        diary(diary_path);
                        
                        try
                            // Official Scilab 2026.0.1 load order:
                            // loadXcosLibs() first, then loadScicos().
                            // Source: help.scilab.org/docs/2026.0.1/en_US/scicos_simulate.html
                            disp('[XcosAI][' + string(LoopID) + '] Loading Xcos libraries...');
                            loadXcosLibs();
                            
                            disp('[XcosAI][' + string(LoopID) + '] Loading Scicos simulation engine...');
                            loadScicos();
                            
                            
                            disp('[XcosAI][' + string(LoopID) + '] Importing diagram...');
                            importXcosDiagram(zcos_path);
                            
                            // Shorten simulation time for rapid parameter validation
                            scs_m.props.tf = 0.1;
                            
                            // ── STEP 0: Graphical Block Substitution ─────────────
                            disp('[XcosAI][' + string(LoopID) + '] Checking for graphical blocks (SCILAB macros)...');
                            n_replaced = 0;
                            replaced_list = '';
                            n_objs = length(scs_m.objs);
                            for i = 1:n_objs
                                try
                                    if typeof(scs_m.objs(i)) == 'Block' then
                                        // model.sim is a list: [fun_name, fun_type]
                                        // type 5 = SCILAB macro
                                        if scs_m.objs(i).model.sim(2) == 5 then
                                            gui_name = scs_m.objs(i).gui;
                                            disp('[XcosAI][' + string(LoopID) + '] Substituting graphical block: ' + gui_name);
                                            scs_m.objs(i).model.sim(1) = 'xcosai_nop_sim';
                                            n_replaced = n_replaced + 1;
                                            if ~strstr(replaced_list, gui_name) then
                                                if replaced_list == '' then replaced_list = gui_name; else replaced_list = replaced_list + ', ' + gui_name; end
                                            end
                                        end
                                    end
                                catch
                                end
                            end
                            if n_replaced > 0 then
                                warning_msg = '[WARN] Graphical blocks substituted for validation: ' + replaced_list;
                                if err_msg == '' then err_msg = warning_msg; else err_msg = warning_msg + ascii(10) + err_msg; end
                            end
                            
                            // POST-IMPORT SANITY CHECK
                            // importXcosDiagram can succeed but still produce an empty
                            // scs_m if the XML structure is not what Scilab expects.
                            // In that case the pre-checks below pass vacuously (no blocks
                            // to iterate) and scicos_simulate crashes with the unhelpful
                            // "scicos_flat: Empty diagram" error.
                            // Catching this early gives Gemini a specific actionable message.
                            n_objs = length(scs_m.objs);
                            n_blocks_found = 0;
                            for i = 1:n_objs
                                try
                                    if typeof(scs_m.objs(i)) == 'Block' then
                                        n_blocks_found = n_blocks_found + 1;
                                    end
                                catch
                                end
                            end
                            disp('[XcosAI][' + string(LoopID) + '] Diagram has ' + string(n_blocks_found) + ' block(s) in scs_m.');
                            if n_blocks_found == 0 then
                                error('Empty diagram: importXcosDiagram loaded the file but scs_m contains no Block objects. The XML is structurally invalid. Check: (1) all blocks use correct XML tags (BasicBlock/RoundBlock/SplitBlock), (2) parent/id attributes are correctly set, (3) the mxGraphModel root structure matches the required skeleton.');
                            end
                            
                            // ── STEP 1: Per-block parameter validation ────────────────
                            // xcosValidateBlockSet runs define+set on each block's
                            // interface function, giving named errors before simulation.
                            // funcprot(0) suppresses "redefining function" warnings that
                            // xcosValidateBlockSet triggers internally (harmless but noisy).
                            // Source: help.scilab.org/docs/2026.0.1/en_US/xcosValidateBlockSet.html
                            disp('[XcosAI][' + string(LoopID) + '] Validating block parameters...');
                            prev_funcprot = funcprot();
                            funcprot(0);
                            
                            block_errors = '';
                            seen_guis = list();
                            
                            for i = 1:n_objs
                                try
                                    obj = scs_m.objs(i);
                                    if typeof(obj) == 'Block' then
                                        gui_name = obj.gui;
                                        already_seen = %f;
                                        for k = 1:length(seen_guis)
                                            if seen_guis(k) == gui_name then
                                                already_seen = %t;
                                                break;
                                            end
                                        end
                                        if ~already_seen then
                                            seen_guis($+1) = gui_name;
                                            [bstatus, bmsg] = xcosValidateBlockSet(gui_name);
                                            if ~bstatus then
                                                block_errors = block_errors + 'Block [' + gui_name + ']: ' + bmsg + ascii(10);
                                                disp('[XcosAI][' + string(LoopID) + '] Block error: ' + gui_name + ': ' + bmsg);
                                            end
                                        end
                                    end
                                catch
                                end
                            end
                            
                            funcprot(prev_funcprot); // Restore previous funcprot setting
                            
                            if block_errors <> '' then
                                err_msg = 'Block parameter validation failed:' + ascii(10) + block_errors;
                                disp('[XcosAI][' + string(LoopID) + '] BLOCK VALIDATION ERRORS:');
                                disp(block_errors);
                            else
                                // ── STEP 2: Link/port connectivity validation ─────────
                                // "Invalid index" from scicos_simulate means a link
                                // references a port index that doesn't exist on a block
                                // (e.g. connecting to output port 2 of a block that only
                                // has 1 output port). Walk every link and cross-check
                                // against actual block port counts to get specific errors.
                                disp('[XcosAI][' + string(LoopID) + '] Validating link connectivity...');
                                link_errors = '';
                                
                                // Build a map: block_id (string) -> Block object
                                // scs_m.objs uses 1-based Scilab indexing
                                block_map_ids   = list();
                                block_map_objs  = list();
                                for i = 1:n_objs
                                    try
                                        obj = scs_m.objs(i);
                                        if typeof(obj) == 'Block' then
                                            block_map_ids($+1)  = obj.id;
                                            block_map_objs($+1) = obj;
                                        end
                                    catch
                                    end
                                end
                                
                                for i = 1:n_objs
                                    try
                                        obj = scs_m.objs(i);
                                        // Links have a 'from' and 'to' field
                                        if typeof(obj) == 'Link' then
                                            // from: [block_id, port_index, port_type]
                                            // port_type: 1=explicit, 2=event
                                            from_id    = string(obj.from(1));
                                            from_port  = obj.from(2);
                                            from_type  = obj.from(3); // 0=out, 1=in on from side
                                            to_id      = string(obj.to(1));
                                            to_port    = obj.to(2);
                                            to_type    = obj.to(3);
                                            
                                            // Find the source block
                                            for k = 1:length(block_map_ids)
                                                if string(block_map_ids(k)) == from_id then
                                                    blk = block_map_objs(k);
                                                    // out ports: model.out, event out: model.evtout
                                                    if from_type == 0 then
                                                        n_ports = size(blk.model.out, 1);
                                                        if from_port > n_ports then
                                                            link_errors = link_errors + ..
                                                                'Link from block [' + blk.gui + '] output port ' + ..
                                                                string(from_port) + ': block only has ' + ..
                                                                string(n_ports) + ' output port(s).' + ascii(10);
                                                        end
                                                    else
                                                        n_ports = size(blk.model.evtout, 1);
                                                        if from_port > n_ports then
                                                            link_errors = link_errors + ..
                                                                'Link from block [' + blk.gui + '] event-out port ' + ..
                                                                string(from_port) + ': block only has ' + ..
                                                                string(n_ports) + ' event-output port(s).' + ascii(10);
                                                        end
                                                    end
                                                    break;
                                                end
                                            end
                                            
                                            // Find the destination block
                                            for k = 1:length(block_map_ids)
                                                if string(block_map_ids(k)) == to_id then
                                                    blk = block_map_objs(k);
                                                    if to_type == 0 then
                                                        n_ports = size(blk.model.in, 1);
                                                        if to_port > n_ports then
                                                            link_errors = link_errors + ..
                                                                'Link to block [' + blk.gui + '] input port ' + ..
                                                                string(to_port) + ': block only has ' + ..
                                                                string(n_ports) + ' input port(s).' + ascii(10);
                                                        end
                                                    else
                                                        n_ports = size(blk.model.evtin, 1);
                                                        if to_port > n_ports then
                                                            link_errors = link_errors + ..
                                                                'Link to block [' + blk.gui + '] event-in port ' + ..
                                                                string(to_port) + ': block only has ' + ..
                                                                string(n_ports) + ' event-input port(s).' + ascii(10);
                                                        end
                                                    end
                                                    break;
                                                end
                                            end
                                        end
                                    catch
                                    end
                                end
                                
                                if link_errors <> '' then
                                    err_msg = 'Link connectivity validation failed:' + ascii(10) + link_errors;
                                    disp('[XcosAI][' + string(LoopID) + '] LINK ERRORS:');
                                    disp(link_errors);
                                else
                                    // ── STEP 3: Full simulation ───────────────────────
                                    // Only run if blocks and links both passed validation.
                                    // Signature: scicos_simulate(scs_m, Info [,context] [,flag])
                                    // Source: help.scilab.org/docs/2026.0.1/en_US/scicos_simulate.html
                                    disp('[XcosAI][' + string(LoopID) + '] Starting simulation (nw mode)...');
                                    scicos_simulate(scs_m, list(), 'nw');
                                    success = %t;
                                    disp('[XcosAI][' + string(LoopID) + '] Simulation COMPLETED.');
                                end
                            end
                            
                        catch
                            [catch_msg, catch_id] = lasterror();
                            // Restore funcprot in case the error happened inside the
                            // validation block before we could restore it
                            try
                                funcprot(1);
                            catch
                            end
                            
                            // Try to read diary to get more context
                            diary_content = '';
                            try
                                diary(0); // Close diary
                                if fileinfo(diary_path) <> [] then
                                    lines = read_csv(diary_path, ascii(10));
                                    diary_content = strcat(lines, ascii(10));
                                end
                            catch
                            end
                            
                            if err_msg <> '' then
                                err_msg = err_msg + ascii(10) + 'Simulation error: ' + catch_msg;
                            else
                                err_msg = catch_msg;
                            end
                            
                            if diary_content <> '' then
                                err_msg = err_msg + ascii(10) + '--- Scilab Console Output ---' + ascii(10) + diary_content;
                            end
                            
                            disp('[XcosAI][' + string(LoopID) + '] VERIFICATION ERROR [' + string(catch_id) + ']: ' + catch_msg);
                        end
                        
                        try
                            diary(0); // Ensure diary is closed
                        catch
                        end
                        
                        // http_post auto-converts a Scilab struct to JSON.
                        // Source: help.scilab.org/docs/2026.0.1/en_US/http_post.html
                        disp('[XcosAI][' + string(LoopID) + '] Posting results...');
                        res_payload = struct('task_id', task_id, 'success', success, 'error', err_msg);
                        [r, s] = http_post(url_base + '/result', res_payload);
                        disp('[XcosAI][' + string(LoopID) + '] Result posted (Status: ' + string(s) + ')');
                    end
                end
            else
                disp('[XcosAI][' + string(LoopID) + '] Server HTTP Error: ' + string(status));
                sleep(2000);
            end
        catch
            disp('[XcosAI][' + string(LoopID) + '] LOOP CRASH: ' + string(lasterror()));
            sleep(2000);
        end
        
        sleep(POLL_MS);
    end
    
    disp('[XcosAI][' + string(LoopID) + '] Polling loop stopped.');
endfunction

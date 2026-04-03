// Scilab ( https://www.scilab.org/ ) - This file is part of Scilab
// Copyright (C) 2025 - Dassault Systèmes S.E. - Adeline CARNIS
//
// For more information, see the COPYING file which you should have received
// along with this program.

function [cluster_centers, labels] = meanshift(data, bandwidth, opts)
    arguments
        data {mustBeA(data, "double")}
        bandwidth {mustBeA(bandwidth, "double"), mustBeScalarOrEmpty, mustBeNonnegative} = []
        opts {mustBeA(opts, "struct")} = struct()
    end

    max_iter = 300
    seeds = []
    bin_seeding = %f
    min_bin_freq = 1
    kernel = "flat";

    fields = fieldnames(opts);
    if fields <> [] then
        expectedFields = ["max_iter", "seeds", "bin_seeding", "min_bin_freq", "kernel"];
        n = members(fields, expectedFields);
        if or(n == 0) then
            error(msprintf(_("%s: Unknown option(s): %s"), "meanshift", sci2exp(fields(n == 0))));
        end

        for f = fields'
            val = opts(f);
            str = sci2exp(f);
            select f
            case {"max_iter", "min_bin_freq"}
                if type(val) <> 1 then
                    error(msprintf(_("%s: Wrong type for %s option: A double expected.\n"), "meanshift", str));
                end
                if ~isscalar(val) then
                    error(msprintf(_("%s: Wrong size for %s option: A scalar expected.\n"), "meanshift", str));
                end

            case "bin_seeding"
                if type(val) <> 4 then
                    error(msprintf(_("%s: Wrong type for %s option: A boolean expected.\n"), "meanshift", str));
                end
                if ~isscalar(val) then
                    error(msprintf(_("%s: Wrong size for %s option: A scalar expected.\n"), "meanshift", str));
                end

            case "seeds"
                if type(val) <> 1 then
                    error(msprintf(_("%s: Wrong type for %s option: A double expected.\n"), "meanshift", str));
                end

            case "kernel"
                if type(val) <> 10 then
                    error(msprintf(_("%s: Wrong type for %s option: A string expected.\n"), "meanshift", str));
                end

                if ~isscalar(val) then
                    error(msprintf(_("%s: Wrong size for %s option: A scalar expected.\n"), "meanshift", str));
                end

                if and(val <> ["flat", "gaussian"]) then
                    error(msprintf(_("%s: Wrong value for %s option: Must be in %s.\n"), "meanshift", str, sci2exp(["flat", "gaussian"])));
                end
            end
            execstr(f + "= val");
        end
    end

    [N, D] = size(data);
    cluster_centers = [];
    labels = zeros(N, 1);
    c = [];

    if N == 0 then
        return;
    end

    if bandwidth == [] then
        bandwidth = estimate_bandwidth(data);
    end

    if seeds == [] then
        if bin_seeding then
            seeds = get_bin_seeds(data, bandwidth, min_bin_freq);
        else
            seeds = data;
        end
    end

    [rows, cols] = size(data);
    o1 = ones(rows, 1);

    [s_points, densities] = %_meanshift(seeds, data, bandwidth, kernel, max_iter);
 
    [all_points, k, l, nb] = unique(s_points, "r", "keepOrder");
    densities = densities(k);
    sorted_points = gsort([densities all_points], "lr")(:, 2:3);
    nbpts = size(sorted_points, 1);
    b = bandwidth ^ 2;
    while nbpts
        m = sorted_points(1,:);
        d = distEuclidian(ones(nbpts, 1) * m, sorted_points);
        idx = find(d <= b);
        cluster_centers = [cluster_centers; m];
        c = [c; o1 * m];
        sorted_points(idx, :) =[];
        nbpts = size(sorted_points, 1);
    end

    // label assignment (each point to the nearest center)
    K = size(cluster_centers, 1);
    if K <> 0 then
        dists = distEuclidian(c, ones(K, 1) .*. data);
        [m, labels] = min(matrix(dists, N, K), "c")
    end

endfunction

function d = distEuclidian(a, b)
    x = a - b;
    d = sum(x.*x, 2);
endfunction

function seeds = get_bin_seeds(X, bandwidth, min_bin_freq)
    coords = round(X ./ bandwidth);
    [u, k, l, nb] = unique(coords, "r");
    idx = find(nb >= min_bin_freq);
    if length(idx) == size(X, 1) then
        seeds = X;
        return
    end
    u = u(idx, :);
    seeds = u .* bandwidth;
endfunction
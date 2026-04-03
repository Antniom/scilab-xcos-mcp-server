// Scilab ( https://www.scilab.org/ ) - This file is part of Scilab
// Copyright (C) 2024 - Dassault Systèmes S.E. - Adeline CARNIS
//
// For more information, see the COPYING file which you should have received
// along with this program.

function [C, centers] = kmeans(X, k)

    arguments
        X {mustBeA(X, "double")}
        k (1,1) {mustBeA(k, "double"), mustBeInteger, mustBePositive}
    end

    function D = euclidianDist(X, Y)
		m = size(X, 1); 
		n = size(Y, 1);
		idx = (1:m) .*. ones(n, 1);
		idy = (1:n)' .*.ones(1, m);
		D = sum((X(idx(:), :) - Y(idy(:), :)) .^ 2, 2);
		D = matrix(sqrt(D), n, m)'
	endfunction

	if k > size(X, 1) then
		error(msprintf(_("%s: Wrong value for input argument #%d: Must be less than or equal to %d.\n"), "kmeans", 2, size(X, 1)));
	end

	maxiter = 100;
	eps = 10^-6;
	t = 0;

	// init classe centers
	n = size(X, 1);
	index = ceil(n * rand(1, k, "uniform"));
	u = unique(index);
	while length(u) <> length(index) then
		index = ceil(n * rand(1, k, "uniform"));
		u = unique(index);
	end

	centers  = X(index, :);
	cv = 1;	

	while (cv > eps && t <= maxiter)
		// assignment step
		D = euclidianDist(X, centers);
		[tmp, C] = min(D, "c");

		// step "recalage" (re-computation of centers)
		for kk = 1:k
			newcenters(kk, :) = mean(X(C == kk, :), 1);
		end

		D = euclidianDist(X, newcenters);
		[tmp, C] = min(D, "c");

		t = t + 1;
		cv = sum((newcenters-centers).^2);
		centers = newcenters;
	end
end
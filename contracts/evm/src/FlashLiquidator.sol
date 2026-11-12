// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "./interfaces/ILiquidatorInterfaces.sol";

/**
 * @title FlashLiquidator
 * @notice Production-grade EVM flash loan executor for Compound v2 / Moonwell liquidations.
 * @dev Enforces:
 *   1. Balance snapshotting: Existing balances cannot mask liquidation losses.
 *   2. Strict callback validation: Only trusted flash loan vault can trigger callback.
 *   3. Zero allowance cleanup: Spender approvals are wiped to 0 immediately after execution.
 *   4. Atomic profit assertion: Reverts if net debt proceeds do not meet configured profit floor.
 *   5. Reentrancy & state machine guard: Rejects unauthorized or nested invocations.
 */
contract FlashLiquidator is IFlashLoanRecipient {
    // Immutables & Config
    address public immutable owner;
    IBalancerVault public immutable trustedVault;
    ISwapRouter public immutable swapRouter;

    // State machine flag
    bool private _inFlashLoan;

    // Execution Context Snapshot
    struct LiquidationParams {
        address borrower;
        address cTokenBorrowed;
        address cTokenCollateral;
        address debtToken;
        address collateralUnderlying;
        uint256 repayAmount;
        uint256 minProfitDebtUnits;
        uint24 swapPoolFee;
        uint256 swapMinAmountOut;
        uint256 deadline;
    }

    // Events
    event LiquidationExecuted(
        address indexed borrower,
        address indexed debtToken,
        address indexed collateralUnderlying,
        uint256 repayAmount,
        uint256 netProfitDebtAsset
    );
    event ProfitsWithdrawn(address indexed token, address indexed recipient, uint256 amount);

    // Errors
    error Unauthorized();
    error CallerNotVault();
    error NotInFlashLoan();
    error Reentrancy();
    error Expired();
    error InactiveProtocol();
    error InsufficientProfit(uint256 netProceeds, uint256 requiredProfit);
    error LiquidationFailed(uint256 errorCode);
    error RedeemFailed(uint256 errorCode);

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    constructor(address _trustedVault, address _swapRouter) {
        require(_trustedVault != address(0), "INVALID_VAULT");
        require(_swapRouter != address(0), "INVALID_ROUTER");
        owner = msg.sender;
        trustedVault = IBalancerVault(_trustedVault);
        swapRouter = ISwapRouter(_swapRouter);
    }

    /**
     * @notice Initiates flash loan and execution cycle.
     * @param params Parameters specifying borrower, assets, amounts, slippage, and profit floor.
     */
    function executeFlashLiquidation(LiquidationParams calldata params) external onlyOwner {
        if (block.timestamp > params.deadline) revert Expired();
        if (_inFlashLoan) revert Reentrancy();

        IERC20[] memory tokens = new IERC20[](1);
        tokens[0] = IERC20(params.debtToken);

        uint256[] memory amounts = new uint256[](1);
        amounts[0] = params.repayAmount;

        bytes memory userData = abi.encode(params);

        _inFlashLoan = true;

        // Snapshot initial debt balance before loan is disbursed
        uint256 initialDebtBalance = IERC20(params.debtToken).balanceOf(address(this));

        // Request flash loan from Balancer V2 Vault
        trustedVault.flashLoan(address(this), tokens, amounts, userData);

        // Verification after loan finishes
        uint256 finalDebtBalance = IERC20(params.debtToken).balanceOf(address(this));
        
        // Assert atomic net profit: final balance must strictly exceed initial balance + min profit
        if (finalDebtBalance < initialDebtBalance + params.minProfitDebtUnits) {
            revert InsufficientProfit(
                finalDebtBalance >= initialDebtBalance ? finalDebtBalance - initialDebtBalance : 0,
                params.minProfitDebtUnits
            );
        }

        _inFlashLoan = false;

        emit LiquidationExecuted(
            params.borrower,
            params.debtToken,
            params.collateralUnderlying,
            params.repayAmount,
            finalDebtBalance - initialDebtBalance
        );
    }

    /**
     * @notice Callback invoked by Balancer Vault upon flash loan transfer.
     */
    function receiveFlashLoan(
        IERC20[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external override {
        // 1. Strict callback validation
        if (msg.sender != address(trustedVault)) revert CallerNotVault();
        if (!_inFlashLoan) revert NotInFlashLoan();

        LiquidationParams memory params = abi.decode(userData, (LiquidationParams));

        address debtToken = address(tokens[0]);
        uint256 repayAmount = amounts[0];
        uint256 feeAmount = feeAmounts[0];

        // 2. Approve cTokenBorrowed to spend the debt token
        IERC20(debtToken).approve(params.cTokenBorrowed, repayAmount);

        // 3. Execute liquidation on lending protocol (Moonwell / Compound v2)
        uint256 err = ICToken(params.cTokenBorrowed).liquidateBorrow(
            params.borrower,
            repayAmount,
            params.cTokenCollateral
        );
        if (err != 0) revert LiquidationFailed(err);

        // Clean up approval to cTokenBorrowed immediately
        IERC20(debtToken).approve(params.cTokenBorrowed, 0);

        // 4. Redeem received cTokenCollateral for underlying collateral token
        uint256 cTokenBalance = ICToken(params.cTokenCollateral).balanceOf(address(this));
        if (cTokenBalance > 0) {
            uint256 redeemErr = ICToken(params.cTokenCollateral).redeem(cTokenBalance);
            if (redeemErr != 0) revert RedeemFailed(redeemErr);
        }

        // 5. Swap redeemed underlying collateral into debt token on SwapRouter
        uint256 collateralBalance = IERC20(params.collateralUnderlying).balanceOf(address(this));
        if (collateralBalance > 0 && params.collateralUnderlying != debtToken) {
            IERC20(params.collateralUnderlying).approve(address(swapRouter), collateralBalance);

            ISwapRouter.ExactInputSingleParams memory swapParams = ISwapRouter.ExactInputSingleParams({
                tokenIn: params.collateralUnderlying,
                tokenOut: debtToken,
                fee: params.swapPoolFee,
                recipient: address(this),
                deadline: params.deadline,
                amountIn: collateralBalance,
                amountOutMinimum: params.swapMinAmountOut,
                sqrtPriceLimitX96: 0
            });

            swapRouter.exactInputSingle(swapParams);

            // Clean up approval to swap router
            IERC20(params.collateralUnderlying).approve(address(swapRouter), 0);
        }

        // 6. Repay flash loan principal + fee back to Vault
        uint256 totalRepayToVault = repayAmount + feeAmount;
        IERC20(debtToken).transfer(address(trustedVault), totalRepayToVault);
    }

    /**
     * @notice Sweep accumulated liquidation profits to treasury.
     */
    function withdraw(address token, address recipient) external onlyOwner {
        require(recipient != address(0), "INVALID_RECIPIENT");
        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance > 0) {
            IERC20(token).transfer(recipient, balance);
            emit ProfitsWithdrawn(token, recipient, balance);
        }
    }
}
